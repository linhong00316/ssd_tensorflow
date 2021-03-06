import tensorflow as tf
from tensorflow.keras.layers import (BatchNormalization, Concatenate, Conv2D,
                                     ReLU, Reshape)

from ssd.layers.decode_predictions import DecodePredictions
from ssd.losses.loss_utils import get_scaled_losses, reduce_losses
from ssd.models.feature_extractors import FeatureExtractors


_policy = tf.keras.mixed_precision.experimental.global_policy()

class SSDModel(tf.keras.Model):

    def __init__(self, config, **kwargs):
        super(SSDModel, self).__init__(**kwargs)
        self._input_height = config['image_height']
        self._input_width = config['image_width']
        self._num_classes = config['num_classes']
        self._num_feature_maps = len(config['feature_shapes'])
        self._feature_shapes = config['feature_shapes']
        self._aspect_ratios = config['aspect_ratios']
        self._backbone = config['backbone']
        self._default_boxs_per_feature_map = [len(x) for x in config['aspect_ratios'][:-1]]
        self._feature_extractors = FeatureExtractors()
        self._decode_predictions = DecodePredictions(config, dtype=_policy.compute_dtype)
        self._freeze_bn = config['freeze_bn']
        self._l2_regularization = config['l2_regularization']
        self._network = self._build_network()

    def compile(self, loss_fn, optimizer, **kwargs):
        super(SSDModel, self).compile(**kwargs)
        self.loss_fn = loss_fn
        self.optimizer = optimizer

    def call(self, x, training):
        is_training = training and (not self._freeze_bn)
        return self._network(x, training=is_training)

    def train_step(self, data):
        images, y_true = data[0], data[1]

        with tf.GradientTape() as tape:
            y_pred = self(images, training=True)
            loss_values = self.loss_fn(y_true, y_pred)
            cls_loss = loss_values[0]
            loc_loss = loss_values[1]

            loss = cls_loss + loc_loss
            regularization_losses = self.losses

            _scaled_losses = get_scaled_losses(loss, regularization_losses)

        gradients = tape.gradient(_scaled_losses, self.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))

        loss_dict = {
            'cls_loss': cls_loss,
            'loc_loss': loc_loss,
            'loss': loss,
            'l2_reg': regularization_losses
        }
        return reduce_losses(loss_dict)

    def test_step(self, data):
        images, y_true = data[0], data[1]

        y_pred = self(images, training=True)
        loss_values = self.loss_fn(y_true, y_pred)
        cls_loss = loss_values[0]
        loc_loss = loss_values[1]

        loss = cls_loss + loc_loss
        regularization_losses = self.losses

        _scaled_losses = get_scaled_losses(loss, regularization_losses)

        loss_dict = {
            'cls_loss': cls_loss,
            'loc_loss': loc_loss,
            'loss': loss,
            'l2_reg': regularization_losses
        }
        return reduce_losses(loss_dict)

    def predict_step(self, data):
        images = data[0]
        predictions = self.call(images, training=False)
        boxes, cls_ids, scores = self._decode_predictions(predictions)
        return {
            'boxes': boxes,
            'cls_ids': cls_ids,
            'scores': scores
        }

    @tf.function
    def get_detections(self, images):
        return self.predict_step((images,))

    def _build_network(self):
        def _conv_block(tensor, filters, k_size, stride, padding, use_bn=False):
            y = Conv2D(filters,
                       k_size,
                       stride,
                       padding,
                       kernel_initializer='he_normal',
                       use_bias=not use_bn)(tensor)
            if use_bn:
                y = BatchNormalization()(y)
            y = ReLU()(y)
            return y

        def _comput_output_shapes():
            num_classes = self._num_classes + 1  # add background class
            output_shape = [0, 4 + num_classes]
            for i in range(self._num_feature_maps):
                grid_locations = self._feature_shapes[i][1] * self._feature_shapes[i][0]
                num_default_boxes = len(self._aspect_ratios[i]) + 1  # 1 addition default box for 1:1 ratio
                output_shape[0] += grid_locations * num_default_boxes
            return output_shape

        def add_l2_regularization(layer):
            l2_reg = tf.keras.regularizers.l2(self._l2_regularization)

            def _add_l2_regularization():
                return l2_reg(layer.kernel)
            return _add_l2_regularization

        input_shape = [self._input_height, self._input_width, 3]
        backbone, feature_maps = self._feature_extractors.model_fns[self._backbone](input_shape)

        image = backbone.input
        x = backbone.output

        x = _conv_block(x, 256, 1, 1, 'same')
        x = _conv_block(x, 256, 3, 2, 'same')
        feature_maps.append(x)

        x = _conv_block(x, 128, 1, 1, 'valid')
        x = _conv_block(x, 256, 3, 1, 'valid')
        feature_maps.append(x)

        x = _conv_block(x, 128, 1, 1, 'valid')
        x = _conv_block(x, 256, 3, 1, 'valid')
        feature_maps.append(x)
        
        if self._num_feature_maps == 7:
            x = _conv_block(x, 256, 1, 1, 'valid')
            x = _conv_block(x, 256, 4, 1, 'valid')
            feature_maps.append(x)

        predictions = []
        output_shape = _comput_output_shapes()

        for i in range(self._num_feature_maps):
            feature_map = feature_maps[i]
            num_default_boxes = len(self._aspect_ratios[i]) + 1  # 1 addition default for 1:1 ratio
            filters = num_default_boxes * output_shape[-1]

            _y = Conv2D(filters=filters,
                        kernel_size=3,
                        padding='same',
                        kernel_initializer='glorot_normal')(feature_map)
            _y = Reshape([-1, output_shape[-1]])(_y)
            predictions.append(_y)
        predictions = Concatenate(axis=1)(predictions)
        model = tf.keras.Model(inputs=[image],
                               outputs=[predictions],
                               name='ssd_network')

        if self._l2_regularization:
            for layer in model.layers:
                if isinstance(layer, Conv2D):
                    if hasattr(layer, 'kernel'):
                        layer.add_loss(add_l2_regularization(layer))

        return model

    def get_inference_network(self):
        image = self._network.input
        predictions = self._network.output
        boxes, cls_ids, scores = self._decode_predictions(predictions)
        return tf.keras.Model(inputs=image,
                              outputs={
                                  'boxes': boxes,
                                  'cls_ids': cls_ids,
                                  'scores': scores
                              },
                              name='ssd_inference_network')
