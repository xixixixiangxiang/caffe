import numpy as np
import os
import os.path as osp
import sys
import glob
import cv2
import multiprocessing as mp
from mincepie import mapreducer, launcher
from argparse import ArgumentParser

pycaffe_dir = osp.dirname(__file__)
if osp.join(pycaffe_dir) not in sys.path:
    sys.path.insert(0, pycaffe_dir)
import caffe
from caffe.proto import caffe_pb2


# Parse arguments
parser = ArgumentParser()
parser.add_argument('--gpu', required=True,
                    help="List of GPUs, e.g., [0,1,2,3]")
parser.add_argument('--input', required=True,
                    help="Path to the image-label list file")
parser.add_argument('--root_dir', default='',
                    help="Path to the images root directory")
parser.add_argument('--model', required=True,
                    help="Path to the deploy prototxt")
parser.add_argument('--weights', required=True,
                    help="Path to the pretrained caffemodel")
parser.add_argument('--input_blob', default='data',
                    help="Input blob name")
parser.add_argument('--output_blob', default='fc',
                    help="Output blob name")
parser.add_argument('--batch_size', type=int, default=1,
                    help="Number of images per batch")
# transformer parameters
parser.add_argument(
    '--mean_file',
    default=osp.join(pycaffe_dir, 'caffe/imagenet/ilsvrc_2012_mean.npy'),
    help="Data set image mean of [Channels x Height x Width] dimensions " +
         "(numpy array). Set to '' for no mean subtraction."
)
parser.add_argument(
    '--resize',
    type=int,
    default=256,
    help="Resize the image so that the shorter side equals to this value."
)
parser.add_argument(
    '--crop_height',
    type=int,
    help="Crop height on the resized image. Only set when using fully conv."
)
parser.add_argument(
    '--crop_width',
    type=int,
    help="Crop width on the resized image. Only set when using fully conv."
)
args = parser.parse_args()
args.gpu = eval(args.gpu)


def _resize(im, siz):
    H, W = im.shape[:2]
    ratio = siz * 1.0 / min(H, W)
    return cv2.resize(im, (0,0), fx=ratio, fy=ratio)


def _crop(im, crop_height, crop_width):
    cy = im.shape[0] / 2.0
    cx = im.shape[1] / 2.0
    top = int(round(cy - crop_height / 2.0))
    bottom = top + crop_height
    left = int(round(cx - crop_width / 2.0))
    right = left + crop_width
    return im[top:bottom, left:right]


def _judge(labels, scores, top_k=[1]):
    assert(len(labels) == scores.shape[0])
    if scores.ndim > 2:
        # fully conv, average over spatial
        scores = scores.mean(axis=tuple(range(2, scores.ndim)))
    labels = np.asarray(labels).reshape(len(labels), 1)
    predictions = np.argsort(scores, axis=1)[:, ::-1]
    correct = (np.tile(labels, (1, predictions.shape[1])) == predictions)
    return [correct[:, :k].sum(axis=1) for k in top_k]


class BatchReader(mapreducer.BasicReader):
    def read(self, input_string):
        inputlist = glob.glob(input_string)
        inputlist.sort()
        data = {}
        batch_index = 0
        for filename in inputlist:
            with open(filename, 'r') as fid:
                lines = fid.readlines()
                for i, line in enumerate(lines):
                    line = line.strip().split()
                    lines[i] = [osp.join(args.root_dir, line[0]), int(line[1])]
                for i in xrange(0, len(lines), args.batch_size):
                    j = min(len(lines), i + args.batch_size)
                    data[batch_index] = lines[i:j]
                    batch_index = batch_index + 1
        return data


class ClassifyMapper(mapreducer.BasicMapper):
    def set_up(self):
        # minus 2, one for the server process, one because the id starts from 1
        self.pid = mp.current_process()._identity[0] - 2
        # initialize the net
        caffe.set_mode_gpu()
        caffe.set_device(args.gpu[self.pid])
        self.net = caffe.Net(args.model, args.weights, caffe.TEST)
        # initialize the transformer
        self.input_shape = list(self.net.blobs[args.input_blob].data.shape)
        if args.crop_height and args.crop_width:
            self.input_shape[2] = args.crop_height
            self.input_shape[3] = args.crop_width
        self.transformer = caffe.io.Transformer(
            {args.input_blob: self.input_shape})
        self.transformer.set_transpose(args.input_blob, (2, 0, 1))
        if args.mean_file:
            mu = np.load(args.mean_file)
            if mu.ndim == 3: mu = mu.mean(1).mean(1)
            self.transformer.set_mean(args.input_blob, mu)

    def map(self, key, value):
        # reshape according to batch size
        self.input_shape[0] = len(value)
        self.net.blobs[args.input_blob].reshape(*self.input_shape)
        # load image and preprocess
        labels = []
        for i, (fpath, label) in enumerate(value):
            im = cv2.imread(fpath)
            im = _resize(im, args.resize)
            im = _crop(im, self.input_shape[2], self.input_shape[3])
            self.net.blobs[args.input_blob].data[i, ...] = \
                self.transformer.preprocess(args.input_blob, im)
            labels.append(label)
        # forward
        self.net.forward()
        out = self.net.blobs[args.output_blob].data
        # get top1, top5 correctness vector
        top1, top5 = _judge(labels, out, top_k=[1,5])
        yield 'top1', top1
        yield 'top5', top5


class ClassifyReducer(mapreducer.BasicReducer):
    def reduce(self, key, values):
        return np.asarray(values).ravel().mean()


class AccuracyWriter(mapreducer.BasicWriter):
    def write(self, result):
        keys = sorted(result.keys())
        for k in keys:
            print k, '{:.2%}'.format(result[k])


mapreducer.REGISTER_DEFAULT_MAPPER(ClassifyMapper)
mapreducer.REGISTER_DEFAULT_REDUCER(ClassifyReducer)
mapreducer.REGISTER_DEFAULT_READER(BatchReader)
mapreducer.REGISTER_DEFAULT_WRITER(AccuracyWriter)


if __name__ == '__main__':
    argv = [sys.argv[0],
            '--input', args.input,
            '--num_clients', str(len(args.gpu))]
    launcher.launch(argv)