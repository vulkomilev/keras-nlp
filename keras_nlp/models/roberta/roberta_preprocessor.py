# Copyright 2022 The KerasNLP Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""RoBERTa preprocessor layer."""

import copy

import tensorflow as tf
import tensorflow_text as tf_text
from tensorflow import keras

from keras_nlp.models.preprocessor import Preprocessor
from keras_nlp.models.roberta.roberta_presets import backbone_presets
from keras_nlp.models.roberta.roberta_tokenizer import RobertaTokenizer
from keras_nlp.utils.keras_utils import (
    convert_inputs_to_list_of_tensor_segments,
)
from keras_nlp.utils.keras_utils import pack_x_y_sample_weight
from keras_nlp.utils.python_utils import classproperty
from keras_nlp.utils.python_utils import format_docstring


@keras.utils.register_keras_serializable(package="keras_nlp")
class RobertaPreprocessor(Preprocessor):
    """RoBERTa preprocessing layer which tokenizes and packs inputs.

    This preprocessing layer will do three things:

    - Tokenize any number of input segments using the `tokenizer`.
    - Pack the inputs together with the appropriate `"<s>"`, `"</s>"` and
      `"<pad>"` tokens, i.e., adding a single `"<s>"` at the start of the
      entire sequence, `"</s></s>"` at the end of each segment, save the last
      and a `"</s>"` at the end of the entire sequence.
    - Construct a dictionary with keys `"token_ids"`, `"segment_ids"`,
       `"padding_mask"`, that can be passed directly to a RoBERTa model.

    This layer can be used directly with `tf.data.Dataset.map` to preprocess
    string data in the `(x, y, sample_weight)` format used by
    `keras.Model.fit`.

    The call method of this layer accepts three arguments, `x`, `y`, and
    `sample_weight`. `x` can be a python string or tensor representing a single
    segment, a list of python strings representing a batch of single segments,
    or a list of tensors representing multiple segments to be packed together.
    `y` and `sample_weight` are both optional, can have any format, and will be
    passed through unaltered.

    Special care should be taken when using `tf.data` to map over an unlabeled
    tuple of string segments. `tf.data.Dataset.map` will unpack this tuple
    directly into the call arguments of this layer, rather than forward all
    argument to `x`. To handle this case, it is recommended to  explicitly call
    the layer, e.g. `ds.map(lambda seg1, seg2: preprocessor(x=(seg1, seg2)))`.

    Args:
        tokenizer: A `keras_nlp.models.RobertaTokenizer` instance.
        sequence_length: The length of the packed inputs.
        truncate: string. The algorithm to truncate a list of batched segments
            to fit within `sequence_length`. The value can be either
            `round_robin` or `waterfall`:
                - `"round_robin"`: Available space is assigned one token at a
                    time in a round-robin fashion to the inputs that still need
                    some, until the limit is reached.
                - `"waterfall"`: The allocation of the budget is done using a
                    "waterfall" algorithm that allocates quota in a
                    left-to-right manner and fills up the buckets until we run
                    out of budget. It supports an arbitrary number of segments.

    Examples:
    ```python
    # Load the preprocessor from a preset.
    preprocessor = keras_nlp.models.RobertaPreprocessor.from_preset("roberta_base_en")

    # Tokenize and pack a single sentence.
    sentence = tf.constant(" afternoon sun")
    preprocessor(sentence)
    # Same output.
    preprocessor(" afternoon sun")

    # Tokenize and a batch of single sentences.
    sentences = tf.constant(
        [" afternoon sun", " night moon"]
    )
    preprocessor(sentences)
    # Same output.
    preprocessor(
        [" afternoon sun", " night moon"]
    )

    # Tokenize and pack a sentence pair.
    first_sentence = tf.constant(" afternoon sun")
    second_sentence = tf.constant("refulgent sun")
    preprocessor((first_sentence, second_sentence))

    # Map a dataset to preprocess a single sentence.
    features = tf.constant(
        [" afternoon sun", " night moon"]
    )
    labels = tf.constant([0, 1])
    ds = tf.data.Dataset.from_tensor_slices((features, labels))
    ds = ds.map(preprocessor, num_parallel_calls=tf.data.AUTOTUNE)

    # Map a dataset to preprocess sentence pairs.
    first_sentences = tf.constant([" afternoon sun", " night moon"])
    second_sentences = tf.constant(["refulgent sun", " bright moon"])
    labels = tf.constant([1, 1])
    ds = tf.data.Dataset.from_tensor_slices(
        (
            (first_sentences, second_sentences), labels
        )
    )
    ds = ds.map(preprocessor, num_parallel_calls=tf.data.AUTOTUNE)

    # Map a dataset to preprocess unlabeled sentence pairs.
    ds = tf.data.Dataset.from_tensor_slices((first_sentences, second_sentences))
    # Watch out for tf.data's default unpacking of tuples here!
    # Best to invoke the `preprocessor` directly in this case.
    ds = ds.map(
        lambda s1, s2: preprocessor(x=(s1, s2)),
        num_parallel_calls=tf.data.AUTOTUNE,
    )

    # Alternatively, you can create a preprocessor from your own vocabulary.
    # The usage is exactly the same as above.
    vocab = {
        "<s>": 0,
        "<pad>": 1,
        "</s>": 2,
        "Ġafter": 5,
        "noon": 6,
        "Ġsun": 7,
    }
    merges = ["Ġ a", "Ġ s", "Ġ n", "e r", "n o", "o n", "Ġs u", "Ġa f", "no on"]
    merges += ["Ġsu n", "Ġaf t", "Ġaft er"]

    tokenizer = keras_nlp.models.RobertaTokenizer(
        vocabulary=vocab,
        merges=merges,
    )
    preprocessor = keras_nlp.models.RobertaPreprocessor(
        tokenizer=tokenizer,
        sequence_length=20,
    )
    ```
    """

    def __init__(
        self,
        tokenizer,
        sequence_length=512,
        truncate="round_robin",
        **kwargs,
    ):
        super().__init__(**kwargs)

        self._tokenizer = tokenizer
        self.packer = RobertaMultiSegmentPacker(
            start_value=self.tokenizer.start_token_id,
            end_value=self.tokenizer.end_token_id,
            pad_value=self.tokenizer.pad_token_id,
            truncate=truncate,
            sequence_length=sequence_length,
        )

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "sequence_length": self.packer.sequence_length,
                "truncate": self.packer.truncate,
            }
        )
        return config

    def call(self, x, y=None, sample_weight=None):
        x = convert_inputs_to_list_of_tensor_segments(x)
        x = [self.tokenizer(segment) for segment in x]
        token_ids = self.packer(x)
        x = {
            "token_ids": token_ids,
            "padding_mask": token_ids != self.tokenizer.pad_token_id,
        }
        return pack_x_y_sample_weight(x, y, sample_weight)

    @classproperty
    def tokenizer_cls(cls):
        return RobertaTokenizer

    @classproperty
    def presets(cls):
        return copy.deepcopy(backbone_presets)

    @classmethod
    def from_preset(cls, preset, **kwargs):
        return super().from_preset(preset, **kwargs)


RobertaPreprocessor.from_preset.__func__.__doc__ = (
    Preprocessor.from_preset.__doc__
)
format_docstring(
    preprocessor_name=RobertaPreprocessor.__name__,
    example_preset_name="roberta_base_en",
    preset_names='", "'.join(RobertaPreprocessor.presets),
)(RobertaPreprocessor.from_preset.__func__)


# TODO: This is a temporary, unexported layer until we find a way to make the
# `MultiSegmentPacker` layer more generic.
@keras.utils.register_keras_serializable(package="keras_nlp")
class RobertaMultiSegmentPacker(keras.layers.Layer):
    """Packs multiple sequences into a single fixed width model input.

    This layer packs multiple input sequences into a single fixed width sequence
    containing start and end delimiters, forming a dense input suitable for a
    classification task for RoBERTa.

    Takes as input a list or tuple of token segments. The layer will process
    inputs as follows:
     - Truncate all input segments to fit within `sequence_length` according to
       the `truncate` strategy.
     - Concatenate all input segments, adding a single `start_value` at the
       start of the entire sequence, `[end_value, end_value]` at the end of
       each segment save the last, and a single `end_value` at the end of the
       entire sequence.
     - Pad the resulting sequence to `sequence_length` using `pad_tokens`.

    Input should be either a `tf.RaggedTensor` or a dense `tf.Tensor`, and
    either rank-1 or rank-2.

    Please refer to the arguments of `keras_nlp.layers.MultiSegmentPacker` for
    more details.
    """

    def __init__(
        self,
        sequence_length,
        start_value,
        end_value,
        pad_value=None,
        truncate="round_robin",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.sequence_length = sequence_length
        if truncate not in ("round_robin", "waterfall"):
            raise ValueError(
                "Only 'round_robin' and 'waterfall' algorithms are "
                "supported. Received %s" % truncate
            )
        self.truncate = truncate
        self.start_value = start_value
        self.end_value = end_value
        self.pad_value = pad_value

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "sequence_length": self.sequence_length,
                "start_value": self.start_value,
                "end_value": self.end_value,
                "pad_value": self.pad_value,
                "truncate": self.truncate,
            }
        )
        return config

    def _trim_inputs(self, inputs):
        """Trim inputs to desired length."""
        # Special tokens include the start token at the beginning of the
        # sequence, two `end_value` at the end of every segment save the last,
        # and the `end_value` at the end of the sequence.
        num_special_tokens = 2 * len(inputs)
        if self.truncate == "round_robin":
            return tf_text.RoundRobinTrimmer(
                self.sequence_length - num_special_tokens
            ).trim(inputs)
        elif self.truncate == "waterfall":
            return tf_text.WaterfallTrimmer(
                self.sequence_length - num_special_tokens
            ).trim(inputs)
        else:
            raise ValueError("Unsupported truncate: %s" % self.truncate)

    def _combine_inputs(self, segments):
        """Combine inputs with start and end values added."""
        dtype = segments[0].dtype
        batch_size = segments[0].nrows()

        start_value = tf.convert_to_tensor(self.start_value, dtype=dtype)
        end_value = tf.convert_to_tensor(self.end_value, dtype=dtype)

        start_column = tf.fill((batch_size, 1), start_value)
        end_column = tf.fill((batch_size, 1), end_value)

        segments_to_combine = []
        for i, seg in enumerate(segments):
            segments_to_combine.append(start_column if i == 0 else end_column)
            segments_to_combine.append(seg)
            segments_to_combine.append(end_column)

        token_ids = tf.concat(segments_to_combine, 1)
        return token_ids

    def call(self, inputs):
        def to_ragged(x):
            return tf.RaggedTensor.from_tensor(x[tf.newaxis, :])

        # If rank 1, add a batch dim.
        rank_1 = inputs[0].shape.rank == 1
        if rank_1:
            inputs = [to_ragged(x) for x in inputs]

        segments = self._trim_inputs(inputs)
        token_ids = self._combine_inputs(segments)
        # Pad to dense tensor output.
        shape = tf.cast([-1, self.sequence_length], tf.int64)
        token_ids = token_ids.to_tensor(
            shape=shape, default_value=self.pad_value
        )
        # Remove the batch dim if added.
        if rank_1:
            token_ids = tf.squeeze(token_ids, 0)

        return token_ids
