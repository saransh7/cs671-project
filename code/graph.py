from includes import config
from includes.utils import pad_sequences, masks, get_answers

from encoder import Encoder
from decoder import Decoder

import numpy as np
from tqdm import tqdm

import tensorflow as tf


CrossEntropy = tf.nn.sparse_softmax_cross_entropy_with_logits
L2Regularizer = tf.contrib.layers.l2_regularizer


class Graph():
    def __init__(self, embeddings, encoder, decoder):

        self.encoder = encoder
        self.decoder = decoder

        self.embeddings = embeddings

        self.init_placeholders()
        self.init_variables()
        self.init_nodes()

        self.saver = tf.train.Saver()
        self.init = tf.global_variables_initializer()

    def init_placeholders(self):
        self.questions_ids = tf.placeholder(
            tf.int32, shape=[None, config.max_question_length]
        )
        self.contexts_ids = tf.placeholder(
            tf.int32, shape=[None, config.max_context_length]
        )

        self.questions_length = tf.placeholder(
            tf.int32, shape=[None]
        )
        self.contexts_length = tf.placeholder(
            tf.int32, shape=[None]
        )

        self.questions_mask = tf.placeholder(
            tf.float32, shape=[None, config.max_question_length]
        )
        self.contexts_mask = tf.placeholder(
            tf.float32, shape=[None, config.max_context_length]
        )

        self.answers = tf.placeholder(
            tf.int32, shape=[None, 2]
        )

        self.labels = tf.placeholder(
            tf.float32, shape=[None, config.n_clusters]
        )

        self.dropout = tf.placeholder(
            tf.float32, shape=[]
        )

        self.w1 = tf.placeholder(
            tf.float32, shape=[]
        )
        self.w2 = tf.placeholder(
            tf.float32, shape=[]
        )

        self.encoder.keep_prob = self.dropout
        self.decoder.keep_prob = self.dropout

    def init_variables(self):
        word_embeddings = tf.Variable(
            self.embeddings, dtype=tf.float32, trainable=config.train_embeddings
        )
        questions_embedding = tf.nn.embedding_lookup(
            word_embeddings,
            self.questions_ids
        )
        contexts_embedding = tf.nn.embedding_lookup(
            word_embeddings,
            self.contexts_ids
        )

        self.questions = tf.nn.dropout(questions_embedding, self.dropout)
        self.contexts = tf.nn.dropout(contexts_embedding, self.dropout)

    def init_nodes(self):
        self.output_attender = self.encoder.encode(
            (self.questions, self.contexts),
            (self.questions_length, self.contexts_length),
            self.questions_mask
        )

        self.logits = self.decoder.decode(
            self.output_attender,
            self.contexts_mask
        )

        labels_shape = tf.shape(self.labels)
        logits_shape = tf.shape(self.logits)

        self.labels_broadcasted = tf.tile(
            tf.reshape(
                tf.transpose(self.labels), [6, 1, labels_shape[0], 1]
            ), tf.stack(
                [1, 2, 1, logits_shape[3]]
            )
        )

        self.predictions = tf.reduce_sum(
            tf.multiply(
                self.labels_broadcasted, self.logits
            ), axis=0
        )

        with tf.variable_scope("loss"):

            self.loss = tf.reduce_mean(
                CrossEntropy(
                    logits=self.predictions[0], labels=self.answers[:, 0]
                ) * self.w1 +
                CrossEntropy(
                    logits=self.predictions[1], labels=self.answers[:, 1]
                ) * self.w2
            )

        learning_rate = tf.train.exponential_decay(
            config.learning_rate,
            0,
            config.decay_steps,
            config.decay_rate,
            staircase=True
        )

        print "\nUsing Adam Optimizer with lr: %f, decay_steps: %d, decay_rate: %f\n" \
            % (config.learning_rate, config.decay_steps, config.decay_rate)

        self.optimizer = tf.train.AdamOptimizer(learning_rate)

        gradients = self.optimizer.compute_gradients(self.loss)
        clipped_gradients = [
            (tf.clip_by_value(grad, -config.max_gradient, config.max_gradient), var)
            for grad, var in gradients
        ]
        grad_check = tf.check_numerics(self.loss, "NaN detected")
        with tf.control_dependencies([grad_check]):
            self.train_step = self.optimizer.apply_gradients(clipped_gradients)

    def init_model(self, sess):
        ckpt = tf.train.get_checkpoint_state(config.train_dir)
        path = ckpt.model_checkpoint_path + ".index" if ckpt else ""

        if config.load_model and ckpt and (
            tf.gfile.Exists(
                ckpt.model_checkpoint_path
            ) or tf.gfile.Exists(path)
        ):
            print "\nInitializing model from %s ... " % ckpt.model_checkpoint_path
            self.saver.restore(sess, ckpt.model_checkpoint_path)

            print "Initialized model\n"

            return True

        else:
            print "\nCreating model with fresh parameters ..."
            sess.run(self.init)
            return False

    def predict(self, sess, dataset, msg):

        answers = []
        ground_answers = []

        if msg != None:
            pbar = tqdm(dataset, desc=msg)
        else:
            pbar = dataset

        for batch in pbar:
            questions_padded, questions_length = pad_sequences(
                batch[:, 0], config.max_question_length
            )
            contexts_padded, contexts_length = pad_sequences(
                batch[:, 1], config.max_context_length
            )

            labels = np.zeros(
                (len(batch), config.n_clusters), dtype=np.float32
            )
            if config.clustering:
                for j, el in enumerate(batch):
                    labels[j, el[3]] = 1
            else:
                labels[:, 0] = 1

            predictions = sess.run(
                self.predictions,
                feed_dict={
                    self.questions_ids: questions_padded,
                    self.questions_length: questions_length,
                    self.questions_mask: masks(questions_length, config.max_question_length),
                    self.contexts_ids: contexts_padded,
                    self.contexts_length: contexts_length,
                    self.contexts_mask: masks(contexts_length, config.max_context_length),
                    self.labels: labels,
                    self.dropout: 1.0
                }
            )

            answers += get_answers(predictions[0], predictions[1])
            ground_answers += [np.array(el[2]) for el in batch]

        return np.array(answers, dtype=np.float32), np.array(ground_answers, dtype=np.float32)

    def save_model(self, sess):
        self.saver.save(sess, "%s/trained_model.chk" % config.train_dir)

    def run_epoch(self, train_dataset, epoch, sess, max_batch_epochs=-1, w1=1.0, w2=1.0):
        print_dict = {"loss": "inf"}

        losses = []

        with tqdm(train_dataset, postfix=print_dict) as pbar:
            pbar.set_description("epoch: %d" % (epoch + 1))
            for i, batch in enumerate(pbar):
                if i == max_batch_epochs:
                    break

                questions_padded, questions_length = pad_sequences(
                    batch[:, 0], config.max_question_length
                )
                contexts_padded, contexts_length = pad_sequences(
                    batch[:, 1], config.max_context_length
                )

                labels = np.zeros(
                    (len(batch), config.n_clusters), dtype=np.float32
                )
                if config.clustering:
                    for j, el in enumerate(batch):
                        labels[j, el[3]] = 1
                else:
                    labels[:, 0] = 1

                try:
                    loss, _ = sess.run(
                        [self.loss, self.train_step],
                        feed_dict={
                            self.questions_ids: questions_padded,
                            self.questions_length: questions_length,
                            self.questions_mask: masks(questions_length, config.max_question_length),
                            self.contexts_ids: contexts_padded,
                            self.contexts_length: contexts_length,
                            self.contexts_mask: masks(contexts_length, config.max_context_length),
                            self.answers: [np.array(el[2]) for el in batch],
                            self.labels: labels,
                            self.w1: w1,
                            self.w2: w2,
                            self.dropout: config.dropout_keep_prob
                        }
                    )

                    print_dict["loss"] = "%.3f" % loss
                    pbar.set_postfix(print_dict)

                    losses.append(loss)

                except Exception as e:
                    # print "NaN detected for batch: " + str(i + 1)
                    pass

            return losses
