#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2020 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import logging
import os
from collections import OrderedDict
from collections import namedtuple
import numpy as np
import tensorflow as tf

from tensorflow.python.platform import gfile
from tensorflow.core.framework import graph_pb2
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import tensor_util
from tensorflow.python.ops import array_ops
from .quantize_graph_common import QuantizeGraphHelper as helper

class QuantizeGraphBase(object):
    """
    This is the base class for quantize graph.
    """

    def __init__(self, output_node_names):
        self.output_node_names = output_node_names
        self.transformers = OrderedDict()

    def register_transformer(self, node_name, entry):
        if node_name not in self.transformers:
            self.transformers[node_name] = []

        self.transformers[node_name].append(entry)

    def do_transform(self):
        """
        This is the virtual interface need to be implemented by derived class
        :return:
        """
        pass

    def remove_dead_nodes(self, input_graph, output_names):
        """Removes nodes that are no longer needed for inference from the graph."""
        return tf.compat.v1.graph_util.extract_sub_graph(
            input_graph, output_names)

    def get_supported_fusion_node(self):
        return self.transformers.keys()


class QuantizeNodeBase(object):
    """This is the base class for nodes fusion


    Arguments:
        object {[type]} -- [description]
    """
    node_details = namedtuple('node_details', ['node', 'input_node', 'output'])

    def __init__(self,
                 input_graph,
                 output_node_names,
                 patterns,
                 per_channel,
                 start_node_name,
                 device,
                 is_asymmetric=False,
                 enable_s8=True):
        self.logger = logging.getLogger()

        if isinstance(input_graph, graph_pb2.GraphDef):
            self.input_graph = input_graph
        else:
            self.input_graph = graph_pb2.GraphDef()
            with gfile.Open(input_graph, 'rb') as f:
                self.input_graph.ParseFromString(f.read())

        self._parse_graph()

        self.output_node_names = output_node_names
        self.output_node_maps = {}
        self.output_graph = graph_pb2.GraphDef()
        self.quantized_node_dict = {}
        self.intel_cpu_eightbitize = True
        self.patterns = patterns
        self.per_channel = per_channel
        self.start_node_name = start_node_name
        self.is_asymmetric = is_asymmetric
        self.device = device
        self.enable_s8 = False if tf.version.VERSION < '2.1.0' and \
            tf.version.VERSION != '1.15.0-up1' else enable_s8

    def apply_the_transform(self):
        """
        This is the virtual interface to be implemented by derived class
        :return: transformed graphdef
        """
        pass

    def get_longest_fuse(self):
        """This is the virtual interface to be implemented by derived class
        """
        pass

    def _is_match(self, patterns):
        """Detect the rule matched nodes collections.

        Returns:
            [List] -- [the matched rule]
            [String] -- [the list contains the matched node name]
        """
        matched_node_name = []

        for k, v in enumerate(self.op_list):
            if v in set(fusion[0] for fusion in patterns):
                cur_node = self.node_name_mapping[list(
                    self.node_name_mapping.keys())[k]].node
                if cur_node.name != self.start_node_name:
                    continue

                if ((v in ("Conv2D", "DepthwiseConv2dNative")
                     and not self.enable_s8)
                    ) and not self._find_relu_node(cur_node):
                    continue

                for sub_rule in patterns:
                    if v != sub_rule[0]:
                        continue

                    sub_rule_len = len(sub_rule)
                    self.logger.debug("Try to apply rule: {}".format(sub_rule))

                    cur_node_name = list(self.node_name_mapping.keys())[k]

                    matched_node_name.clear()

                    matched_node_name.append(cur_node_name)

                    while sub_rule_len > 1:
                        if not self.node_name_mapping[cur_node_name].output:
                            self.logger.debug(
                                "Failed to match {}".format(sub_rule))
                            break

                        next_node_name = self.node_name_mapping[
                            cur_node_name].output[0]

                        next_node_op = self.node_name_mapping[
                            next_node_name].node.op

                        add_op_quantizable = True

                        if next_node_op in ("Add", "AddN"):
                            next_node = self.node_name_mapping[
                                next_node_name].node
                            next_node_inputs = list(next_node.input)
                            cur_node_index = next_node_inputs.index(
                                cur_node_name)

                            for index, input_name in enumerate(
                                    next_node_inputs):
                                node_type = self.node_name_mapping[helper.node_name_from_input(
                                    input_name)].node.op
                                if input_name != cur_node_name and index < cur_node_index and \
                                        node_type != 'Dequantize':
                                    add_op_quantizable = False
                                    break

                        is_shared_output = True if len(
                            self.node_name_mapping[cur_node_name].output
                        ) > 1 else False

                        if add_op_quantizable and not is_shared_output and \
                                next_node_op == sub_rule[1 - sub_rule_len]:
                            matched_node_name.append(next_node_name)
                            sub_rule_len -= 1
                            cur_node_name = next_node_name
                        else:
                            matched_node_name.clear()
                            self.logger.debug(
                                "Failed to match {}".format(sub_rule))
                            break

                    if sub_rule_len == 1:
                        self.logger.debug("match {} on nodes {} ".format(
                            sub_rule, matched_node_name))
                        return sub_rule, matched_node_name

        return None, None

    def _need_to_check(self, node_type):
        op_list = ("ConcatV2", "Conv2D", "DepthwiseConv2D", "QuantizeV2",
                   "DepthwiseConv2dNative", "MaxPool", "Requantize", "AvgPool",
                   "Pad", "CropAndResize", "Dequantize", "Mean", "MatMul")
        return any([node_type.find(i) != -1 for i in op_list])

    def _find_relu_node(self, node):
        if node.op in ("Relu", "Relu6") or node.op.find("AndRelu") != -1:
            return True
        elif (node.op.find("QuantizedConv") != -1
              or node.op.find("QuantizedDepthwiseConv") != -1 or
              node.op.find("QuantizedMatMul") != -1
              ) and node.op.find("Relu") == -1:
            return False
        elif self._need_to_check(node.op):
            input_node = self.node_name_mapping[helper.node_name_from_input(
                node.input[0])]
            return self._find_relu_node(input_node.node)
        else:
            return False

    def _add_output_node(self, node_name, node):
        if node_name not in self.output_node_maps:
            self.output_node_maps[node_name] = node
        else:
            raise ValueError("Duplicate Node Found {} {} {}".format(
                node_name, node.op, self.output_node_maps[node_name].op))

    def _reset_output_node_maps(self):
        self.output_node_maps = {}

    def write_graph(self, out_graph_def, out_graph_file):
        """Write output graphDef to file.

        :param out_graph_def: output graphDef.
        :param out_graph_file: path to output graph file.
        :return: None.
        """
        if not isinstance(out_graph_def, tf.compat.v1.GraphDef):
            raise ValueError(
                'out_graph_def is not instance of TensorFlow GraphDef.')
        if out_graph_file and not os.path.exists(
                os.path.dirname(out_graph_file)):
            raise ValueError('"output_graph" directory does not exists.')
        f = gfile.GFile(out_graph_file, 'wb')
        f.write(out_graph_def.SerializeToString())

    def _get_op_list(self):
        self.op_list = []
        for _, v in enumerate(self.node_name_mapping):
            self.op_list.append(self.node_name_mapping[v].node.op)

    def _get_node_input(self, node_name):
        """
        Return control_input name, non-control_input node name
        """

        return [
            i for i in self.node_name_mapping[node_name].node.input
            if i[0] == '^'
        ], [
            i for i in self.node_name_mapping[node_name].node.input
            if i[0] != '^'
        ]

    def _intel_cpu_add_dequantize_result_node(self,
                                              quantized_output_name,
                                              original_node_name,
                                              dtype=dtypes.quint8,
                                              min_tensor_index=1):
        min_max_inputs = [
            "%s:%s" % (quantized_output_name, min_tensor_index),
            "%s:%s" % (quantized_output_name, min_tensor_index + 1)
        ]
        dequantize_name = original_node_name

        dequantize_node = helper.create_node(
            "Dequantize", dequantize_name,
            [quantized_output_name, min_max_inputs[0], min_max_inputs[1]])
        helper.set_attr_dtype(dequantize_node, "T", dtype)
        helper.set_attr_string(dequantize_node, "mode",
                               b"MIN_FIRST" if self.is_asymmetric else b"SCALED")
        self.add_output_graph_node(dequantize_node)

    def eightbitize_single_input_tensor_node(self, original_node,
                                             add_op_function):
        quantized_op_name = original_node.name + "_eightbit_quantized"
        quantized_op_type = "Quantized" + original_node.op
        all_input_names = self._add_eightbit_prologue_nodes(original_node.name)
        quantized_op_node = helper.create_node(quantized_op_type,
                                               quantized_op_name,
                                               all_input_names)
        add_op_function(original_node, quantized_op_node)
        self.add_output_graph_node(quantized_op_node)
        self._intel_cpu_add_dequantize_result_node(quantized_op_name,
                                                   original_node.name)

    def _add_eightbit_prologue_nodes(self, original_node):
        namespace_prefix = original_node + "_eightbit"
        reshape_dims_name, reduction_dims_name = self._add_common_quantization_nodes(
            namespace_prefix, helper.node_name_from_input(
                self.node_name_mapping[original_node].node.input[0]))
        input_names = []
        min_max_names = []
        for each_input_name in self.node_name_mapping[
                original_node].node.input:
            if each_input_name[0] == '^':
                continue
            input_node_name = helper.node_name_from_input(each_input_name)
            if self.intel_cpu_eightbitize and input_node_name in self.output_node_maps:
                # dtype = dtypes.DType(
                #     self.output_node_maps[input_node_name].attr["T"].type
                # ) if self.output_node_maps[
                #     input_node_name].op == "Dequantize" else dtypes.quint8

                if self.output_node_maps[input_node_name].op == "Dequantize":
                    dtype = dtypes.DType(
                        self.output_node_maps[input_node_name].attr["T"].type)
                elif self._find_relu_node(
                        self.node_name_mapping[original_node].node):
                    dtype = dtypes.quint8
                elif self.node_name_mapping[original_node].node.op == "MatMul":
                    # mkl ops _MklQuantizedMatMulWithBiasAndRelu|AndRequantize
                    # requires the T1 data type as quint8
                    dtype = dtypes.quint8
                else:
                    dtype = dtypes.qint8
            else:
                dtype = dtypes.quint8 if self._find_relu_node(
                    self.node_name_mapping[original_node].node
                ) else dtypes.qint8

            quantize_input_name, min_input_name, max_input_name = (
                self._eightbitize_input_to_node(namespace_prefix,
                                                each_input_name,
                                                reshape_dims_name,
                                                reduction_dims_name,
                                                dtype=dtype))
            input_names.append(quantize_input_name)
            min_max_names.append(min_input_name)
            min_max_names.append(max_input_name)
        all_input_names = []
        all_input_names.extend(input_names)
        all_input_names.extend(min_max_names)

        for original_input_name in self.node_name_mapping[
                original_node].node.input:
            if original_input_name[0] == '^':
                all_input_names.append(original_input_name)
        return all_input_names

    def _add_common_quantization_nodes(self,
                                       namespace_prefix,
                                       control_input_names=None):
        """Builds constant nodes needed for quantization of inputs."""
        reshape_dims_name = namespace_prefix + "_reshape_dims"
        reduction_dims_name = namespace_prefix + "_reduction_dims"

        reshape_dims_node = helper.create_constant_node(
            reshape_dims_name, -1, dtypes.int32, [1])
        if control_input_names:
            reshape_dims_node.input.append("^" + control_input_names)

        self.add_output_graph_node(reshape_dims_node)
        reduction_dims_node = helper.create_constant_node(
            reduction_dims_name, 0, dtypes.int32, [1])
        if control_input_names:
            reduction_dims_node.input.append("^" + control_input_names)
        self.add_output_graph_node(reduction_dims_node)
        return reshape_dims_name, reduction_dims_name

    def add_output_graph_node(self, output_node):
        """Inserts one node into the new graph."""
        self.output_graph.node.extend([output_node])
        self._add_output_node(output_node.name, output_node)

    def _parse_graph(self, input_graph=None):
        """
        Parse the graph and get the input node and output node name details.
        """
        self.logger.debug("start parsing graph")
        self.node_name_mapping = OrderedDict()

        graph = self.input_graph if input_graph is None else input_graph
        for node in graph.node:
            each_node = self.node_details(node=node, input_node=[], output=[])

            if node.name in self.node_name_mapping:
                raise ValueError(
                    "Duplicate Node Found when _parse_graph, the node name is {}" .format(
                        node.name))

            self.node_name_mapping[node.name] = each_node

        for node in graph.node:
            for input in node.input:
                self.node_name_mapping[helper.node_name_from_input(
                    input)].output.append(node.name)

    def remove_redundant_quantization(self, old_graph):
        old_nodes_map = self.create_nodes_map(old_graph)
        self.output_graph = graph_pb2.GraphDef()
        inputs_to_rename = {}
        # We go through all the nodes, looking for any that match the patterns we
        # know how to optimize away.
        for node in old_graph.node:
            # We always start with a Quantize node, and examine its inputs to see if
            # they are in a form that can be removed.
            if node.op not in ["Quantize", "QuantizeV2"]:
                continue

            dequantize_node_name = helper.node_name_from_input(node.input[0])
            if dequantize_node_name not in old_nodes_map:
                raise ValueError("Input node name '" + dequantize_node_name +
                                 "' not found in node '" + node.name + "'")
            dequantize_node = old_nodes_map[dequantize_node_name]
            # Do we have a Dequantize feeding in, with the same type as the
            # Quantize?
            if dequantize_node.op != "Dequantize":
                continue

            if node.attr["T"] != dequantize_node.attr["T"]:
                continue

            # Now look at the other inputs, and ensure they're Min/Max nodes.
            min_node_name = helper.node_name_from_input(node.input[1])
            max_node_name = helper.node_name_from_input(node.input[2])
            min_node = old_nodes_map[min_node_name]
            max_node = old_nodes_map[max_node_name]
            is_min_right_type = (min_node.op in ["Min", "Dequantize"])
            is_max_right_type = (max_node.op in ["Max", "Dequantize"])
            if not is_min_right_type or not is_max_right_type:
                self.logger.info("Didn't find expected types on inputs : %s, %s." %
                                 (min_node.op, max_node.op))
                continue
            min_node_input_name = helper.node_name_from_input(
                min_node.input[0])
            max_node_input_name = helper.node_name_from_input(
                max_node.input[0])
            # There are two different patterns for Min nodes we can recognize, one
            # where the input comes directly from the same one as the Max, and
            # another where we run it through another Min first, so check for
            # both.
            is_same_input = False
            if min_node_input_name == max_node_input_name:
                is_same_input = True
            else:
                first_min_node_input = old_nodes_map[min_node_input_name]
                if first_min_node_input.op == "Concat":
                    second_min_node_name = helper.node_name_from_input(
                        first_min_node_input.input[1])
                    second_min_node = old_nodes_map[second_min_node_name]
                    if second_min_node.op == "Min":
                        second_min_node_input_name = helper.node_name_from_input(
                            second_min_node.input[0])
                        is_same_input = (
                            second_min_node_input_name == max_node_input_name)
            if not is_same_input:
                self.logger.info("Different min/max inputs: " + min_node_input_name)
                continue
            # We recognize this pattern, so mark the graph edges to be rewired to
            # route around it entirely, since we know it's a no-op.
            dequantize_source_name = helper.node_name_from_input(
                dequantize_node.input[0])
            node_tensor_name = helper.ensure_tensor_name_has_port(node.name)
            min_tensor_name = node.name + ":1"
            max_tensor_name = node.name + ":2"

            inputs_to_rename[node_tensor_name] = dequantize_source_name
            inputs_to_rename[min_tensor_name] = dequantize_node.input[1]
            inputs_to_rename[max_tensor_name] = dequantize_node.input[2]
        # Finally we apply all the rewiring we've marked to the graph.
        for node in old_graph.node:
            for index, input_full_name in enumerate(node.input):
                input_name = helper.ensure_tensor_name_has_port(
                    input_full_name)
                if input_name in inputs_to_rename:
                    node.input[index] = inputs_to_rename[input_name]
            self.add_output_graph_node(node)
        return self.output_graph

    def create_nodes_map(self, graph):
        """Builds a mapping of node names to their defs from the graph."""
        nodes_map = {}
        for node in graph.node:
            if node.name not in nodes_map.keys():
                nodes_map[node.name] = node
            else:
                raise ValueError("Duplicate node names detected.")

        return nodes_map

    def _add_quantize_down_nodes(self,
                                 original_node,
                                 quantized_output_name,
                                 requantize_type=dtypes.quint8,
                                 is_relu6=False):
        quantized_outputs = [
            quantized_output_name, quantized_output_name + ":1",
            quantized_output_name + ":2"
        ]
        # Add a RequantizationRange node for finding the min and max values.
        requant_range_node = helper.create_node(
            "RequantizationRangePerChannel"
            if self.per_channel else "RequantizationRange",
            original_node.name + "_eightbit_requant_range", quantized_outputs)

        if self.per_channel:
            helper.set_attr_dtype(requant_range_node, "T", dtypes.qint32)
            if is_relu6:
                helper.set_attr_float(requant_range_node, "clip_value_max",
                                      6.0)
            else:
                helper.set_attr_float(requant_range_node, "clip_value_max",
                                      1e30)
        else:
            helper.set_attr_dtype(requant_range_node, "Tinput", dtypes.qint32)

        self.add_output_graph_node(requant_range_node)
        min_max_inputs = [
            requant_range_node.name + ":0", requant_range_node.name + ":1"
        ]
        requantize_node = helper.create_node(
            "RequantizePerChannel" if self.per_channel else "Requantize",
            original_node.name + "_eightbit_requantize",
            quantized_outputs + min_max_inputs)
        if self.per_channel:
            helper.set_attr_dtype(requantize_node, "T", dtypes.qint32)
        else:
            helper.set_attr_dtype(requantize_node, "Tinput", dtypes.qint32)

        helper.set_attr_dtype(requantize_node, "out_type", requantize_type)
        self.add_output_graph_node(requantize_node)
        return requantize_node.name

    def _eightbitize_input_to_node(self,
                                   namespace_prefix,
                                   original_input_name,
                                   reshape_dims_name,
                                   reduction_dims_name,
                                   dtype=dtypes.quint8):
        """Takes one float input to an op, and converts it to quantized form."""
        unique_input_name = helper.unique_node_name_from_input(
            original_input_name)
        if unique_input_name in self.quantized_node_dict:
            quantized_tuple = self.quantized_node_dict[unique_input_name]
            return quantized_tuple[0], quantized_tuple[1], quantized_tuple[2]

        reshape_input_name = namespace_prefix + "_reshape_" + unique_input_name
        min_input_name = namespace_prefix + "_min_" + unique_input_name
        max_input_name = namespace_prefix + "_max_" + unique_input_name
        quantize_input_name = namespace_prefix + "_quantize_" + unique_input_name
        reshape_input_node = helper.create_node(
            "Reshape", reshape_input_name,
            [original_input_name, reshape_dims_name])
        helper.set_attr_dtype(reshape_input_node, "T", dtypes.float32)
        self.add_output_graph_node(reshape_input_node)
        min_input_node = helper.create_node(
            "Min", min_input_name, [reshape_input_name, reduction_dims_name])
        helper.set_attr_dtype(min_input_node, "T", dtypes.float32)
        helper.set_attr_dtype(min_input_node, "Tidx", dtypes.int32)
        helper.set_attr_bool(min_input_node, "keep_dims", False)
        self.add_output_graph_node(min_input_node)
        max_input_node = helper.create_node(
            "Max", max_input_name, [reshape_input_name, reduction_dims_name])
        helper.set_attr_dtype(max_input_node, "T", dtypes.float32)
        helper.set_attr_dtype(max_input_node, "Tidx", dtypes.int32)
        helper.set_attr_bool(max_input_node, "keep_dims", False)
        self.add_output_graph_node(max_input_node)
        quantize_input_node = helper.create_node(
            "QuantizeV2", quantize_input_name,
            [original_input_name, min_input_name, max_input_name])

        helper.set_attr_dtype(quantize_input_node, "T", dtype)

        helper.set_attr_string(quantize_input_node, "mode",
                               b"MIN_FIRST" if self.is_asymmetric else b"SCALED")
        if not self.is_asymmetric:
            helper.set_attr_string(quantize_input_node, "round_mode",
                                   b"HALF_TO_EVEN")
        # if FLAGS.model_name in ["wide_deep_large_ds"]:
        #    set_attr_string(quantize_input_node, "mode", b"MIN_FIRST")
        # else:
        #    set_attr_string(quantize_input_node, "mode",
        #                    b"SCALED" if self.intel_cpu_eightbitize else b"MIN_FIRST")
        #    set_attr_string(quantize_input_node, "round_mode",
        #                    b"HALF_TO_EVEN" if self.intel_cpu_eightbitize
        #                    else b"HALF_AWAY_FROM_ZERO")
        self.add_output_graph_node(quantize_input_node)
        min_output_name = quantize_input_name + ":1"
        max_output_name = quantize_input_name + ":2"
        self.quantized_node_dict[unique_input_name] = (quantize_input_name,
                                                       min_output_name,
                                                       max_output_name)
        return quantize_input_name, min_output_name, max_output_name

    def _intel_cpu_quantize_weight_eightbit(self,
                                            parent,
                                            input_node,
                                            per_channel,
                                            quantization_mode=b"SCALED"):
        base_name = input_node.name + "_"
        qint8_const_name = base_name + "qint8_const"
        min_name = base_name + "min"
        max_name = base_name + "max"
        float_tensor = tensor_util.MakeNdarray(input_node.attr["value"].tensor)
        epsilon = 1e-4  # Needs to be set empirically if accuracy is not satisfactory
        if parent in ("Conv2D", "MatMul"):
            if per_channel:
                ranges = np.abs(float_tensor).max(axis=(0, 1, 2))
                min_value = -ranges
                max_value = ranges
                # nudging min-max values outside epsilon radius around zero
                ranges[ranges < epsilon] = epsilon
                min_value[np.abs(min_value) < epsilon] = -epsilon
                max_value[np.abs(max_value) < epsilon] = epsilon
                qint8_tensor = (float_tensor * 127.0 / ranges).astype(np.int8)
            else:
                min_value = np.min(float_tensor.flatten())
                max_value = np.max(float_tensor.flatten())
                # Same processing of min-max as in quantize_weight_eightbit
                # function.
                if min_value > 0.0:
                    min_value = 0.0
                if min_value == max_value:
                    if abs(min_value) < 0.000001:
                        max_value = min_value + 1.0
                    elif min_value > 0:
                        max_value = 2 * min_value
                    else:
                        max_value = min_value / 2.0

                sess = tf.compat.v1.Session()
                with sess.as_default():
                    quantize_op = array_ops.quantize_v2(
                        float_tensor,
                        min_value,
                        max_value,
                        dtypes.qint8,
                        mode=quantization_mode,
                        round_mode="HALF_TO_EVEN")
                    qint8_tensor = quantize_op[0].numpy(
                    ) if tf.executing_eagerly() else quantize_op[0].eval()
                    # Updated min-max values should be passed to the next
                    # feeding node.
                    min_value = quantize_op[1].numpy(
                    ) if tf.executing_eagerly() else quantize_op[1].eval()
                    max_value = quantize_op[2].numpy(
                    ) if tf.executing_eagerly() else quantize_op[2].eval()
                sess.close()
        elif parent == "DepthwiseConv2dNative":
            # get the max values based on dim 0 and 1 for depthwise conv
            # since, the output channel will be dim 2 * dim 3
            ranges = np.abs(float_tensor).max(axis=(0, 1))
            ranges = ranges.flatten()
            min_value = -ranges
            max_value = ranges
            # nudging min-max values outside epsilon radius around zero
            ranges[ranges < epsilon] = epsilon
            min_value[np.abs(min_value) < epsilon] = -epsilon
            max_value[np.abs(max_value) < epsilon] = epsilon
            # Since output channel will be 1 dim which is dim 2 * dim 3
            # When divide by range, qint8_tensor needs to be 3 dim
            # where, 3rd dim should be same dim of ranges
            a, b, c, d = float_tensor.shape
            qint8_tensor = (float_tensor.reshape(a, b, c * d) * 127.0 /
                            ranges).astype(np.int8)
            # get the shape back to 4 dim
            qint8_tensor = qint8_tensor.reshape(a, b, c, d)
        shape = tensor_util.TensorShapeProtoToList(
            input_node.attr["value"].tensor.tensor_shape)
        qint8_const_node = helper.create_constant_node(qint8_const_name,
                                                       qint8_tensor,
                                                       dtypes.qint8,
                                                       shape=shape)

        min_node = helper.create_constant_node(min_name, min_value,
                                               dtypes.float32, device=self.device)

        max_node = helper.create_constant_node(max_name, max_value,
                                               dtypes.float32, device=self.device)

        dequantize_node = helper.create_node(
            "Dequantize", input_node.name,
            [qint8_const_name, min_name, max_name])

        helper.set_attr_dtype(dequantize_node, "T", dtypes.qint8)
        helper.set_attr_string(dequantize_node, "mode", b"SCALED")
        self.add_output_graph_node(qint8_const_node)
        self.add_output_graph_node(min_node)
        self.add_output_graph_node(max_node)
        self.add_output_graph_node(dequantize_node)
