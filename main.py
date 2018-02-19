#!/usr/bin/env python
# -*- coding: utf-8 -*-

from collections import defaultdict
from itertools import zip_longest
from tqdm import tqdm
import argparse
import gzip
import lzma
import numpy as np
import time

from torch import nn
from torch import optim
from torch.autograd import Variable
import torch
import torch.nn.functional as F

from common import init_logger

verbose = False
logger = None
use_cuda = False

DIM_EMB = 50


class CBOW(nn.Module):
    """Continuous bag-of-words model."""
    def __init__(self, vocab_size, dim_emb=DIM_EMB, *args, **kwargs):
        """Initialize a network."""
        super(CBOW, self).__init__()
        self.verbose = kwargs.get('verbose', False)
        self.logger = init_logger('CBOW')
        self.embeddings_x = nn.Embedding(vocab_size, dim_emb, sparse=True)
        self.embeddings_y = nn.Embedding(vocab_size, dim_emb, sparse=True)

    def forward(self, X, y):
        """Forward calculation."""
        context = self.embeddings_x(X).mean(dim=1)
        target = self.embeddings_y(y)
        return torch.mul(target, context).sum(dim=1)

    def forward_neg(self, X, y):
        """Forward calculation for nagative samples."""
        context = self.embeddings_x(X).mean(dim=1)  # (batch_size, dim_embed)
        context = context.unsqueeze(dim=2)  # (batch_size, dim_embed, 1)
        target = self.embeddings_y(y)  # (batch_size, neg_sample_size, dim_emb)
        # Output: (batch_size, neg_sample_size)
        return torch.bmm(target, context).squeeze(dim=2)

    def get_embeddings(self):
        """Return embeddings."""
        dat = self.embeddings_x.weight.data
        if use_cuda:
            dat = dat.cpu()
        return dat.numpy()


class Corpus():
    """Corpus reader."""
    def __init__(self, window_size, *args, **kwargs):
        "Set variables."
        self.verbose = kwargs.get('verbose', False)
        self.logger = init_logger('Corpus')
        self.window_size = window_size  # length of window on each side

        # Vocabulary
        self.w2i = defaultdict(lambda: len(self.w2i))
        self.freq = defaultdict(int)

        self.UNK = self.w2i['<unk>']
        self.BOS = self.w2i['<s>']
        self.EOS = self.w2i['</s>']
        self.freq[self.UNK] = 0
        self.freq[self.BOS] = 0
        self.freq[self.EOS] = 0
        self.vocab_range = {}

    def set_i2w(self):
        """Initialize an i2w indexer."""
        self.i2w = [w for w, _ in sorted(self.w2i.items(),
                                         key=lambda t: t[1])]

    def read(self, path_corpus, lang, count=True):
        """Read a corpus and convert it into a matrix of word indices."""
        if path_corpus.endswith('.xz'):
            f = lzma.open(path_corpus, 'rt')
        elif path_corpus.endswith('.gz'):
            f = gzip.open(path_corpus, 'rt')
        else:
            f = open(path_corpus, 'rt')
        if self.verbose:
            self.logger.info('Read from ' + path_corpus)
        vocab_start = len(self.w2i)
        for line in f:
            words = line.strip().split()
            if len(words) < 2 * self.window_size - 1:
                continue
            # e.g., en:apple
            indices = [self.w2i[lang + ':' + w] for w in words]
            if count:
                for idx in indices:
                    self.freq[idx] += 1
            yield [self.BOS] + indices + [self.EOS]
        if self.verbose:
            self.logger.info('Done.')
        f.close()

        # Record vocabulary range
        vocab_end = len(self.w2i)
        self.vocab_range[lang] = (vocab_start, vocab_end)

    def get_vocabsize(self):
        return len(self.w2i)


def generate_batch(sents, window, batch_size):
    """Generate batches."""
    contexts, targets, count = [], [], 0
    count = 0
    for sent in sents:
        l = len(sent)
        for pos in range(window, l - window):
            context = sent[pos - window:pos]
            context += sent[pos + 1:pos + window + 1]
            contexts.append(context)
            targets.append(sent[pos])
            count += 1
            if count >= batch_size:
                contexts = Variable(torch.LongTensor(contexts))
                targets = Variable(torch.LongTensor(targets))
                if use_cuda:
                    contexts = contexts.cuda()
                    targets = targets.cuda()
                yield contexts, targets
                contexts, targets, count = [], [], 0


class NegativeSamplingLoss():
    def __init__(self, freq, ranges, src='en', trg='it', sample_size=5):
        """Initialize a sampler."""
        self.calc_prior(freq, ranges, src, trg)
        self.sample_size = sample_size

    def calc_prior(self, freq, ranges, src='en', trg='it'):
        """Compute sampling prior."""
        self.p = torch.FloatTensor(
            [v for i, v in sorted(freq.items(), key=lambda t: t[1])])
        self.p **= 0.75
        # Language specific prior
        self.p_src = self.p[:ranges[src][1]].clone()
        self.p_trg = self.p[ranges[trg][0]:].clone()
        self.p_src /= self.p_src.sum()
        self.p_trg /= self.p_trg.sum()
        self.offset_trg = ranges[trg][0]

    def __call__(self, model, contexts, targets, src=True):
        """Compute the loss value."""
        if src:
            p_mono, p_cross = self.p_src, self.p_trg
        else:
            p_mono, p_cross = self.p_trg, self.p_src
        B = contexts.size(0)  # batch size
        # Positive samples
        loss = model.forward(contexts, targets).sigmoid().log().sum()

        # Negative samples
        n_neg = B * self.sample_size
        targets_neg = [
            torch.multinomial(p_mono, n_neg, replacement=True).view((B, -1)),
            torch.multinomial(p_cross, n_neg, replacement=True).view((B, -1))]
        targets_neg = Variable(torch.cat(targets_neg, dim=1))
        if use_cuda:
            targets_neg = targets_neg.cuda()
        loss += model.forward_neg(contexts, targets_neg).neg().sigmoid().log().sum()

        # TODO: trick on cross-lingual negative sampling

        return loss / B


class DistributionLoss():
    def __init__(self, dim_emb, lambda_m=0.2, lambda_v=0.1):
        self.m = {l: torch.zeros(dim_emb) for l in ['src', 'trg']}
        self.v = {l: torch.zeros((dim_emb, dim_emb)) for l in ['src', 'trg']}
        self.scount = {l: 0 for l in ['src', 'trg']}
        self.updated = {l: False for l in ['src', 'trg']}
        self.lambda_m = lambda_m
        self.lambda_v = lambda_v

    def __call__(self, model, x, src=True):
        """Calculate a distribution loss."""
        if src:
            self.update_stats(model, x, 'src')
        else:
            self.update_stats(model, x, 'trg')

        if not (self.updated['src'] and self.updated['trg']):
            # Not updated yet
            return None

        loss = self.lambda_m * (self.m['src'] - self.m['trg']).pow(2).sum() / 2
        loss += self.lambda_v * (self.v['src'] - self.v['trg']).pow(2).sum() / 2
        return loss

    def update_stats(self, model, x, lang):
        """Update a mean vector and a covariance matrix.

        In the paper, the update formula is defined w.r.t. one instance.
        But contexts contain multiple words, and it will be more reasonable to
        update the statistics w.r.t. the whole context."""
        x = x.squeeze(dim=0)  # -> (window_size * 2, dim_embed)
        N = x.size(0)

        # Aggregate vectors (different from the paper)
        vec = model.embeddings_x(x).sum(dim=0).squeeze().data  # -> (dim_embed)

        # Mean
        new_m = vec
        if self.m[lang] is not None:
            new_m += self.scount[lang] * self.m[lang]
        self.m[lang] = new_m / (self.scount[lang] + N)  # in the paper, N = 1

        # Covariance
        diff = (self.m[lang] - vec).view((-1, 1))
        new_v = diff.mul(diff.t())
        if self.v[lang] is not None:
            new_v += self.scount[lang] * self.v[lang]
        self.v[lang] = new_v / (self.scount[lang] + N)

        self.scount[lang] = min(100000, self.scount[lang] + N)
        self.updated[lang] = True


def save_embeddings(filename, embs, i2w):
    if verbose:
        logger.info('Save embeddings to ' + filename)
    with open(filename, 'w') as f:
        f.write('{} {}\n'.format(*embs.shape))
        for w, emb in zip(i2w, embs):
            f.write('{} {}\n'.format(w, ' '.join(str(v) for v in emb)))


def main(args):
    global verbose
    verbose = args.verbose

    global use_cuda
    use_cuda = args.cuda

    if torch.cuda.is_available() and use_cuda:
        torch.cuda.manual_seed(args.random_seed)
    np.random.seed(args.random_seed)

    lr = args.lr
    batch_size = args.batch_size
    window_size = args.window_size

    # Load data
    lang_src, path_src = args.path_src.split(':')
    lang_trg, path_trg = args.path_trg.split(':')
    corpus = Corpus(window_size=window_size, verbose=verbose)
    sents_src = list(corpus.read(path_src, lang=lang_src))
    sents_trg = list(corpus.read(path_trg, lang=lang_trg))
    corpus.set_i2w()

    model = CBOW(corpus.get_vocabsize(), verbose=verbose)
    if use_cuda:
        model.cuda()
    loss_func = NegativeSamplingLoss(corpus.freq, corpus.vocab_range,
                                     src=lang_src, trg=lang_trg)
    loss_func_dist = DistributionLoss(DIM_EMB)
    optimizer = optim.SGD(model.parameters(), lr=lr)

    for ITER in range(args.n_iters):
        train_loss = 0.0
        start = time.time()
        np.random.shuffle(sents_src)
        batches_src = generate_batch(sents_src, window_size, batch_size)
        np.random.shuffle(sents_trg)
        batches_trg = generate_batch(sents_trg, window_size, batch_size)
        for batch_src, batch_trg in tqdm(zip_longest(batches_src, batches_trg)):
            for batch, src in [(batch_src, True), (batch_trg, False)]:
                if batch is None:  # no instances in the batch
                    continue
                model.zero_grad()
                contexts, targets = batch
                loss = loss_func(model, contexts, targets, src=src)
                loss_d = loss_func_dist(model, contexts, src=src)
                if loss_d is not None:  # distribution loss
                    loss += loss_d
                loss.backward()
                optimizer.step()
                train_loss += float(loss.data)
        print('[{}] loss = {:.4f}, time = {:.2f}'.format(
            ITER+1, train_loss, time.time() - start))

    # Save vectors
    embs = model.get_embeddings()
    save_embeddings(args.path_output, embs, corpus.i2w)
    return 0


if __name__ == '__main__':
    logger = init_logger('MAIN')
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--src', dest='path_src',
                        required=True, help='path to a source corpus file')
    parser.add_argument('-t', '--trg', dest='path_trg',
                        required=True, help='path to a target corpus file')
    parser.add_argument('--window-size', type=int, default=2,
                        help='window size on each side')
    parser.add_argument('--lr', type=float, default=0.01,
                        help='learning rate')
    parser.add_argument('--batch-size', type=int, default=1024,
                        help='batch size')
    parser.add_argument('--iter', dest='n_iters', type=int, default=5,
                        help='number of iterations')
    parser.add_argument('--seed', dest='random_seed', type=int, default=42,
                        help='random seed')
    parser.add_argument('-o', '--output', dest='path_output',
                        required=True, help='path to an output file')
    parser.add_argument('--cuda', action='store_true', default=False,
                        help='use CUDA')
    parser.add_argument('-v', '--verbose',
                        action='store_true', default=False,
                        help='verbose output')
    args = parser.parse_args()
    main(args)
