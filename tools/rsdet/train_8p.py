# -*- coding:utf-8 -*-

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import os
import sys
import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
sys.path.append("../../")

from tools.train_base import Train
from libs.configs import cfgs
from libs.models.detectors.rsdet import build_whole_network_8p
from libs.utils.coordinate_convert import backward_convert, get_horizen_minAreaRectangle

os.environ["CUDA_VISIBLE_DEVICES"] = cfgs.GPU_GROUP


class TrainRSDet(Train):

    def get_gtboxes_and_label(self, gtboxes_and_label_h, gtboxes_and_label_r, num_objects):
        return gtboxes_and_label_h[:int(num_objects), :].astype(np.float32), \
               gtboxes_and_label_r[:int(num_objects), :].astype(np.float32)

    def main(self):
        with tf.Graph().as_default() as graph, tf.device('/cpu:0'):

            num_gpu = len(cfgs.GPU_GROUP.strip().split(','))
            global_step = slim.get_or_create_global_step()
            lr = self.warmup_lr(cfgs.LR, global_step, cfgs.WARM_SETP, num_gpu)
            tf.summary.scalar('lr', lr)

            optimizer = tf.train.MomentumOptimizer(lr, momentum=cfgs.MOMENTUM)
            rsdet = build_whole_network_8p.DetectionNetworkRSDet(cfgs=self.cfgs,
                                                                 is_training=True)

            with tf.name_scope('get_batch'):
                if cfgs.IMAGE_PYRAMID:
                    shortside_len_list = tf.constant(cfgs.IMG_SHORT_SIDE_LEN)
                    shortside_len = tf.random_shuffle(shortside_len_list)[0]

                else:
                    shortside_len = cfgs.IMG_SHORT_SIDE_LEN

                img_name_batch, img_batch, gtboxes_and_label_batch, num_objects_batch, img_h_batch, img_w_batch = \
                    self.reader.next_batch(dataset_name=cfgs.DATASET_NAME,
                                           batch_size=cfgs.BATCH_SIZE * num_gpu,
                                           shortside_len=shortside_len,
                                           is_training=True)

            # data processing
            inputs_list = []
            for i in range(num_gpu):
                img = tf.expand_dims(img_batch[i], axis=0)
                if cfgs.NET_NAME in ['resnet152_v1d', 'resnet101_v1d', 'resnet50_v1d']:
                    img = img / tf.constant([cfgs.PIXEL_STD])

                gtboxes_and_label_q = tf.reshape(gtboxes_and_label_batch[i], [-1, 9])

                gtboxes_and_label_h = get_horizen_minAreaRectangle(gtboxes_and_label_batch[i])
                gtboxes_and_label_h = tf.reshape(gtboxes_and_label_h, [-1, 5])

                num_objects = num_objects_batch[i]
                num_objects = tf.cast(tf.reshape(num_objects, [-1, ]), tf.float32)

                img_h = img_h_batch[i]
                img_w = img_w_batch[i]

                inputs_list.append([img, gtboxes_and_label_h, gtboxes_and_label_q, num_objects, img_h, img_w])

            tower_grads = []
            biases_regularizer = tf.no_regularizer
            weights_regularizer = tf.contrib.layers.l2_regularizer(cfgs.WEIGHT_DECAY)

            with tf.variable_scope(tf.get_variable_scope()):
                for i in range(num_gpu):
                    with tf.device('/gpu:%d' % i):
                        with tf.name_scope('tower_%d' % i):
                            with slim.arg_scope(
                                    [slim.model_variable, slim.variable],
                                    device='/device:CPU:0'):
                                with slim.arg_scope([slim.conv2d, slim.conv2d_in_plane,
                                                     slim.conv2d_transpose, slim.separable_conv2d,
                                                     slim.fully_connected],
                                                    weights_regularizer=weights_regularizer,
                                                    biases_regularizer=biases_regularizer,
                                                    biases_initializer=tf.constant_initializer(0.0)):

                                    gtboxes_and_label_h, gtboxes_and_label_q = tf.py_func(self.get_gtboxes_and_label,
                                                                                          inp=[inputs_list[i][1],
                                                                                               inputs_list[i][2],
                                                                                               inputs_list[i][3]],
                                                                                          Tout=[tf.float32, tf.float32])
                                    gtboxes_and_label_h = tf.reshape(gtboxes_and_label_h, [-1, 5])
                                    gtboxes_and_label_q = tf.reshape(gtboxes_and_label_q, [-1, 9])

                                    img = inputs_list[i][0]
                                    img_shape = inputs_list[i][-2:]
                                    img = tf.image.crop_to_bounding_box(image=img,
                                                                        offset_height=0,
                                                                        offset_width=0,
                                                                        target_height=tf.cast(img_shape[0], tf.int32),
                                                                        target_width=tf.cast(img_shape[1], tf.int32))

                                    outputs = rsdet.build_whole_detection_network(input_img_batch=img,
                                                                                  gtboxes_batch_h=gtboxes_and_label_h,
                                                                                  gtboxes_batch_r=gtboxes_and_label_q,
                                                                                  gpu_id=i)
                                    gtboxes_in_img_h = self.drawer.draw_boxes_with_categories(img_batch=img,
                                                                                              boxes=gtboxes_and_label_h[
                                                                                                    :, :-1],
                                                                                              labels=gtboxes_and_label_h[
                                                                                                     :, -1],
                                                                                              method=0)
                                    gtboxes_in_img_q = self.drawer.draw_boxes_with_categories(img_batch=img,
                                                                                              boxes=gtboxes_and_label_q[
                                                                                                    :, :-1],
                                                                                              labels=gtboxes_and_label_q[
                                                                                                     :, -1],
                                                                                              method=2)
                                    tf.summary.image('Compare/gtboxes_h_gpu:%d' % i, gtboxes_in_img_h)
                                    tf.summary.image('Compare/gtboxes_q_gpu:%d' % i, gtboxes_in_img_q)

                                    if cfgs.ADD_BOX_IN_TENSORBOARD:
                                        detections_in_img = self.drawer.draw_boxes_with_categories_and_scores(
                                            img_batch=img,
                                            boxes=outputs[0],
                                            scores=outputs[1],
                                            labels=outputs[2],
                                            method=1)
                                        tf.summary.image('Compare/final_detection_gpu:%d' % i, detections_in_img)

                                    loss_dict = outputs[-1]
                                    total_loss_dict, total_losses = self.loss_dict(loss_dict, num_gpu)

                                    if i == num_gpu - 1:
                                        regularization_losses = tf.get_collection(
                                            tf.GraphKeys.REGULARIZATION_LOSSES)
                                        # weight_decay_loss = tf.add_n(slim.losses.get_regularization_losses())
                                        total_losses = total_losses + tf.add_n(regularization_losses)

                            tf.get_variable_scope().reuse_variables()
                            grads = optimizer.compute_gradients(total_losses)
                            if cfgs.GRADIENT_CLIPPING_BY_NORM is not None:
                                grads = slim.learning.clip_gradient_norms(grads, cfgs.GRADIENT_CLIPPING_BY_NORM)
                            tower_grads.append(grads)
            self.log_printer(rsdet, optimizer, global_step, tower_grads, total_loss_dict, num_gpu, graph)

if __name__ == '__main__':

    trainer = TrainRSDet(cfgs)
    trainer.main()