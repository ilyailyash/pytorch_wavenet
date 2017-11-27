import fnmatch
import os
import random
import re
import threading
import Queue

import librosa
import numpy as np
import torch

FILE_PATTERN = r'p([0-9]+)_([0-9]+)\.wav'

def get_category_cardinality(files):
    id_reg_expression = re.compile(FILE_PATTERN)
    min_id = None
    max_id = None
    for filename in files:
        matches = id_reg_expression.findall(filename)[0]
        id, recording_id = [int(id_) for id_ in matches]
        if min_id is None or id < min_id:
            min_id = id
        if max_id is None or id > max_id:
            max_id = id

    return min_id, max_id


def randomize_files(files):
    for file in files:
        file_index = random.randint(0, (len(files) - 1))
        yield files[file_index]


def find_files(directory, pattern='*.wav'):
    '''Recursively finds all files matching the pattern.'''
    files = []
    for root, dirnames, filenames in os.walk(directory):
        for filename in fnmatch.filter(filenames, pattern):
            files.append(os.path.join(root, filename))
    return files


def load_generic_audio(directory, sample_rate, amount):
    '''Generator that yields audio waveforms from the directory.'''
    files = find_files(directory)
    id_reg_exp = re.compile(FILE_PATTERN)
    randomized_files = randomize_files(files)
    for it, filename in enumerate(randomized_files):
        if it == amount:
            break
        ids = id_reg_exp.findall(filename)
        if not ids:
            # The file name does not match the pattern containing ids, so
            # there is no id.
            category_id = None
        else:
            # The file name matches the pattern for containing ids.
            category_id = int(ids[0][0])
        audio, _ = librosa.load(filename, sr=sample_rate, mono=True)
        audio = audio.reshape(-1, 1)
        yield audio, filename, category_id


def trim_silence(audio, threshold, frame_length=2048):
    '''Removes silence at the beginning and end of a sample.'''
    if audio.size < frame_length:
        frame_length = audio.size
    energy = librosa.feature.rmse(audio, frame_length=frame_length)
    frames = np.nonzero(energy > threshold)
    indices = librosa.core.frames_to_samples(frames)[1]

    # Note: indices can be an empty array, if the whole audio was silence.
    return audio[indices[0]:indices[-1]] if indices.size else audio[0:0]


def not_all_have_id(files):
    ''' Return true iff any of the filenames does not conform to the pattern
        we require for determining the category id.'''
    id_reg_exp = re.compile(FILE_PATTERN)
    for file in files:
        ids = id_reg_exp.findall(file)
        if not ids:
            return True
    return False


class AudioReader(object):
    '''Generic background audio reader that preprocesses audio files
    and enqueues them into a TensorFlow queue.'''

    def __init__(self,
                 audio_dir,
                 sample_rate,
                 gc_enabled,
                 receptive_field,
                 sample_size=None,
                 silence_threshold=None,
                 load_size=64):
        self.audio_dir = audio_dir
        self.sample_rate = sample_rate
        self.sample_size = sample_size
        self.receptive_field = receptive_field
        self.silence_threshold = silence_threshold
        self.gc_enabled = gc_enabled
        self.data_set = Queue.Queue()
        self.load_size = load_size
        
        if self.gc_enabled:
            self.id_placeholder = tf.placeholder(dtype=tf.int32, shape=())
            self.gc_queue = tf.PaddingFIFOQueue(queue_size, ['int32'],
                                                shapes=[()])
            self.gc_enqueue = self.gc_queue.enqueue([self.id_placeholder])

        # TODO Find a better way to check this.
        # Checking inside the AudioReader's thread makes it hard to terminate
        # the execution of the script, so we do it in the constructor for now.
        files = find_files(audio_dir)
        if not files:
            raise ValueError("No audio files found in '{}'.".format(audio_dir))
        if self.gc_enabled and not_all_have_id(files):
            raise ValueError("Global conditioning is enabled, but file names "
                             "do not conform to pattern having id.")
        # Determine the number of mutually-exclusive categories we will
        # accomodate in our embedding table.
        if self.gc_enabled:
            _, self.gc_category_cardinality = get_category_cardinality(files)
            # Add one to the largest index to get the number of categories,
            # since tf.nn.embedding_lookup expects zero-indexing. This
            # means one or more at the bottom correspond to unused entries
            # in the embedding lookup table. But that's a small waste of memory
            # to keep the code simpler, and preserves correspondance between
            # the id one specifies when generating, and the ids in the
            # file names.
            self.gc_category_cardinality += 1
            print("Detected --gc_cardinality={}".format(
                  self.gc_category_cardinality))
        else:
            self.gc_category_cardinality = None

    ''' 
    def dequeue(self, num_elements):
        output = list()
        for i in xrange (num_elements):    
            output.append(autograd.Variable(torch.FloatTensor(self.queue.get())).cuda())
        return output

    def dequeue_gc(self, num_elements):
        return self.gc_queue.dequeue_many(num_elements)
    '''

    def thread_main(self):
        # Go through the dataset multiple times
        
        iterator = load_generic_audio(self.audio_dir, self.sample_rate, self.load_size)
        for audio, filename, category_id in iterator:
            if self.silence_threshold is not None:
                # Remove silence
                audio = trim_silence(audio[:, 0], self.silence_threshold)
                audio = audio.reshape(-1, 1)
                if audio.size == 0:
                    print("Warning: {} was ignored as it contains only "
                          "silence. Consider decreasing trim_silence "
                          "threshold, or adjust volume of the audio."
                          .format(filename))

            audio = np.pad(audio, [[self.receptive_field, 0], [0, 0]],
                           'constant')

            if self.sample_size:
                # Cut samples into pieces of size receptive_field +
                # sample_size with receptive_field overlap
                i = 0
                while len(audio) > self.receptive_field:
                    piece = audio[:(self.receptive_field +
                                    self.sample_size), :]
                    self.data_set.put(piece)
                    audio = audio[self.sample_size:, :]
                    i += 1
                    if self.gc_enabled:
                        sess.run(self.gc_enqueue, feed_dict={
                                self.id_placeholder: category_id})
            else:
                self.data_set.put(audio)
                if self.gc_enabled:
                    sess.run(self.gc_enqueue,
                             feed_dict={self.id_placeholder: category_id})
        return self.data_set.qsize()