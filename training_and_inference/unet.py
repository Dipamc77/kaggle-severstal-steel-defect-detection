""" 
Copy of unet code from https://github.com/qubvel/segmentation_models 

Changelog:
Added new upsampling block type - transpose3layer
Made small modifications for some extra options

""""

from keras_applications import get_submodules_from_kwargs
import keras
import tensorflow as tf

from unet_utils import Conv2dBn, freeze_model
from segmentation_models.backbones.backbones_factory import Backbones

backend = keras.backend
layers = keras.layers
models = keras.models
keras_utils = keras.utils


# ---------------------------------------------------------------------
#  Utility functions
# ---------------------------------------------------------------------

def get_submodules():
    return {
        'backend': backend,
        'models': models,
        'layers': layers,
        'utils': keras_utils,
    }


# ---------------------------------------------------------------------
#  Blocks
# ---------------------------------------------------------------------

def Conv3x3BnACT(filters, use_batchnorm, name=None, activation='relu'):
    kwargs = get_submodules()

    def wrapper(input_tensor):
        return Conv2dBn(
            filters,
            kernel_size=3,
            activation=activation,
            kernel_initializer='he_uniform',
            padding='same',
            use_batchnorm=use_batchnorm,
            name=name,
            **kwargs
        )(input_tensor)

    return wrapper


def DecoderUpsamplingX2Block(filters, stage, use_batchnorm=False, decoder_activation='relu'):
    up_name = 'decoder_stage{}_upsampling'.format(stage)
    conv1_name = 'decoder_stage{}a'.format(stage)
    conv2_name = 'decoder_stage{}b'.format(stage)
    concat_name = 'decoder_stage{}_concat'.format(stage)

    concat_axis = 3 if backend.image_data_format() == 'channels_last' else 1

    def wrapper(input_tensor, skip=None):
        x = layers.UpSampling2D(size=2, name=up_name)(input_tensor)

        if skip is not None:
            x = layers.Concatenate(axis=concat_axis, name=concat_name)([x, skip])

        x = Conv3x3BnACT(filters, use_batchnorm, name=conv1_name, activation=decoder_activation)(x)
        x = Conv3x3BnACT(filters, use_batchnorm, name=conv2_name, activation=decoder_activation)(x)

        return x

    return wrapper


def DecoderTransposeX2Block(filters, stage, use_batchnorm=False, decoder_activation='relu'):
    transp_name = 'decoder_stage{}a_transpose'.format(stage)
    bn_name = 'decoder_stage{}a_bn'.format(stage)
    act_name = 'decoder_stage{}a_{}'.format(stage,decoder_activation)
    conv_block_name = 'decoder_stage{}b'.format(stage)
    concat_name = 'decoder_stage{}_concat'.format(stage)

    concat_axis = bn_axis = 3 if backend.image_data_format() == 'channels_last' else 1

    def layer(input_tensor, skip=None):

        x = layers.Conv2DTranspose(
            filters,
            kernel_size=(4, 4),
            strides=(2, 2),
            padding='same',
            name=transp_name,
            use_bias=not use_batchnorm,
        )(input_tensor)

        if use_batchnorm:
            x = layers.BatchNormalization(axis=bn_axis, name=bn_name)(x)

        x = layers.Activation(decoder_activation, name=act_name)(x)

        if skip is not None:
            x = layers.Concatenate(axis=concat_axis, name=concat_name)([x, skip])
        else:
            _, inp_h, inp_w, _ = input_tensor.get_shape().as_list()
            ch = x.get_shape().as_list()[-1]
            x = layers.Reshape([inp_h*2, inp_w*2, ch])(x)

        x = Conv3x3BnACT(filters, use_batchnorm, name=conv_block_name, activation=decoder_activation)(x)
        
        return x

    return layer

def DecoderTransposeX2_3Layer(filters, stage, use_batchnorm=False, decoder_activation='relu'):
    transp_name = 'decoder_stage{}a_transpose'.format(stage)
    bn_name = 'decoder_stage{}a_bn'.format(stage)
    if isinstance(decoder_activation, str):
        act_name = 'decoder_stage{}a_{}'.format(stage, decoder_activation)
    else:
        act_name = 'decoder_stage{}a_{}'.format(stage, decoder_activation.__name__)
    conv_block1_name = 'decoder_stage{}b1'.format(stage)
    conv_block2_name = 'decoder_stage{}b2'.format(stage)
    concat_name = 'decoder_stage{}_concat'.format(stage)

    concat_axis = bn_axis = 3 if backend.image_data_format() == 'channels_last' else 1

    def layer(input_tensor, skip=None):

        
        x = layers.Conv2DTranspose(
            filters,
            kernel_size=(4, 4),
            strides=(2, 2),
            padding='same',
            name=transp_name,
            use_bias=not use_batchnorm,
        )(input_tensor)

        if use_batchnorm:
            x = layers.BatchNormalization(axis=bn_axis, name=bn_name)(x)

        x = layers.Activation(decoder_activation, name=act_name)(x)

        if skip is not None:
            x = layers.Concatenate(axis=concat_axis, name=concat_name)([x, skip])
        else:
            _, inp_h, inp_w, _ = input_tensor.get_shape().as_list()
            ch = x.get_shape().as_list()[-1]
            x = layers.Reshape([inp_h*2, inp_w*2, ch])(x) # Hack to fix shape error in kaggle, not needed for inference

        x = Conv3x3BnACT(filters//2, use_batchnorm, name=conv_block1_name, activation=decoder_activation)(x)
        x = Conv3x3BnACT(filters//2, use_batchnorm, name=conv_block2_name, activation=decoder_activation)(x)
        return x

    return layer


# ---------------------------------------------------------------------
#  Unet Decoder
# ---------------------------------------------------------------------

def build_unet(
        backbone,
        decoder_block,
        skip_connection_layers,
        decoder_filters=(256, 128, 64, 32, 16),
        decoder_activation='relu',
        n_upsample_blocks=5,
        classes=1,
        activation='sigmoid',
        use_batchnorm=True,
        output_name='final_activation'
):
    input_ = backbone.input
    x = backbone.output

    # extract skip connections
    skips = ([backbone.get_layer(name=i).output if isinstance(i, str)
              else backbone.get_layer(index=i).output for i in skip_connection_layers])

    # add center block if previous operation was maxpooling (for vgg models)
    if isinstance(backbone.layers[-1], layers.MaxPooling2D):
        x = Conv3x3BnACT(512, use_batchnorm, name='center_block1')(x) # default relu activation for center blocks
        x = Conv3x3BnACT(512, use_batchnorm, name='center_block2')(x)

    # building decoder blocks
    for i in range(n_upsample_blocks):

        if i < len(skips):
            skip = skips[i]
        else:
            skip = None

        x = decoder_block(decoder_filters[i], stage=i, use_batchnorm=use_batchnorm, decoder_activation=decoder_activation)(x, skip)

    # model head (define number of output classes)
    x = layers.Conv2D(
        filters=classes,
        kernel_size=(3, 3),
        padding='same',
        use_bias=True,
        kernel_initializer='glorot_uniform',
        name='final_conv',
    )(x)
    x = layers.Activation(activation, name=output_name)(x)

    # create keras model instance
    model = models.Model(input_, x)

    return model


# ---------------------------------------------------------------------
#  Unet Model
# ---------------------------------------------------------------------

def Unet(
        backbone_name='vgg16',
        input_shape=(None, None, 3),
        classes=1,
        activation='sigmoid',
        weights=None,
        encoder_weights='imagenet',
        encoder_freeze=False,
        decoder_activation='relu',
        encoder_features='default',
        decoder_block_type='upsampling',
        decoder_filters=(256, 128, 64, 32, 16),
        decoder_use_batchnorm=True,
        output_name='final_activation',
        **kwargs
):
    """ Unet_ is a fully convolution neural network for image semantic segmentation

    Args:
        backbone_name: name of classification model (without last dense layers) used as feature
            extractor to build segmentation model.
        input_shape: shape of input data/image ``(H, W, C)``, in general
            case you do not need to set ``H`` and ``W`` shapes, just pass ``(None, None, C)`` to make your model be
            able to process images af any size, but ``H`` and ``W`` of input images should be divisible by factor ``32``.
        classes: a number of classes for output (output shape - ``(h, w, classes)``).
        activation: name of one of ``keras.activations`` for last model layer
            (e.g. ``sigmoid``, ``softmax``, ``linear``).
        weights: optional, path to model weights.
        encoder_weights: one of ``None`` (random initialization), ``imagenet`` (pre-training on ImageNet).
        encoder_freeze: if ``True`` set all layers of encoder (backbone model) as non-trainable.
        encoder_features: a list of layer numbers or names starting from top of the model.
            Each of these layers will be concatenated with corresponding decoder block. If ``default`` is used
            layer names are taken from ``DEFAULT_SKIP_CONNECTIONS``.
        decoder_block_type: one of blocks with following layers structure:

            - `upsampling`:  ``UpSampling2D`` -> ``Conv2D`` -> ``Conv2D``
            - `transpose`:   ``Transpose2D`` -> ``Conv2D``
            - `transpose3layer`: ``Transpose2D`` -> ``Conv2D`` -> ``Conv2D``


        decoder_filters: list of numbers of ``Conv2D`` layer filters in decoder blocks
        decoder_use_batchnorm: if ``True``, ``BatchNormalisation`` layer between ``Conv2D`` and ``Activation`` layers
            is used.

    Returns:
        ``keras.models.Model``: **Unet**

    .. _Unet:
        https://arxiv.org/pdf/1505.04597

    """

    #global backend, layers, models, keras_utils
    #backend, layers, models, keras_utils = get_submodules_from_kwargs(kwargs)
    kwargs['backend'] = backend
    kwargs['layers'] = layers
    kwargs['models'] = models
    kwargs['utils'] = keras_utils


    if decoder_block_type == 'upsampling':
        decoder_block = DecoderUpsamplingX2Block
    elif decoder_block_type == 'transpose':
        decoder_block = DecoderTransposeX2Block
    elif decoder_block_type == 'transpose3layer':
        decoder_block = DecoderTransposeX2_3Layer
    else:
        raise ValueError('Decoder block type should be in ("upsampling", "transpose", "transpose3layer"). '
                         'Got: {}'.format(decoder_block_type))

    backbone = Backbones.get_backbone(
        backbone_name,
        input_shape=input_shape,
        weights=encoder_weights,
        include_top=False,
        **kwargs,
    )

    if encoder_features == 'default':
        encoder_features = Backbones.get_feature_layers(backbone_name, n=4)

    model = build_unet(
        backbone=backbone,
        decoder_block=decoder_block,
        skip_connection_layers=encoder_features,
        decoder_filters=decoder_filters,
        classes=classes,
        activation=activation,
        decoder_activation=decoder_activation,
        n_upsample_blocks=len(decoder_filters),
        use_batchnorm=decoder_use_batchnorm,
        output_name=output_name,
    )

    # lock encoder weights for fine-tuning
    if encoder_freeze:
        freeze_model(backbone, **kwargs)

    # loading model weights
    if weights is not None:
        model.load_weights(weights)

    return model
