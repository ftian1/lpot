from tensorflow import keras
import numpy as np

class Dataset(object):
  def __init__(self):
      (train_images, train_labels), (test_images,
                 test_labels) = keras.datasets.fashion_mnist.load_data()
      self.test_images = test_images.astype(np.float32) / 255.0
      self.labels = test_labels
      pass

  def __getitem__(self, index):
      return self.test_images[index], self.labels[index]

  def __len__(self):
      return len(self.test_images)

# Define a customized Metric function 
import lpot
from lpot.metric import Metric
class MyMetric(Metric):
  def __init__(self, *args):
      self.pred_list = []
      self.label_list = []
      self.samples = 0
      pass

  def update(self, predict, label):
      self.pred_list.extend(np.argmax(predict, axis=1))
      self.label_list.extend(label)
      self.samples += len(label) 
      pass

  def reset(self):
      self.pred_list = []
      self.label_list = []
      self.samples = 0
      pass

  def result(self):
      correct_num = np.sum(
            np.array(self.pred_list) == np.array(self.label_list))
      return correct_num / self.samples


# Quantize with customized dataloader and metric
quantizer = lpot.Quantization('./conf.yaml')
dataset = Dataset()
quantizer.metric('hello_metric', MyMetric) 
dataloader = quantizer.dataloader(dataset, batch_size=1)
q_model = quantizer('../models/simple_model', q_dataloader = dataloader, eval_dataloader = dataloader)

# Optional, run quantized model
import tensorflow as tf
with tf.compat.v1.Graph().as_default(), tf.compat.v1.Session() as sess:
     tf.compat.v1.import_graph_def(q_model.as_graph_def(), name='')
     styled_image = sess.run(['output:0'], feed_dict={'input:0':dataset.test_images})
     print("Inference is done.")

