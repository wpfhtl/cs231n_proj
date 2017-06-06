from __future__ import print_function, division

import glob
import os
from scipy.misc import imread, imsave

import torch
import torch.nn as nn
import torch.optim as optim
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torch.utils.data import TensorDataset
from torch.utils.data import sampler

import torchvision.datasets as dset
import torchvision.transforms as T

import numpy as np

from normalizer import normalize, denorm
from encodedecode import EncodeDecode
from viewmorphing import ViewMorphing
from directgen import EncodeDecodeDirect

NUM_TRAIN = 64#16000
NUM_VAL = 16
NUM_SAVED_SAMPLES = 16
BATCH_SIZE = 64
DATA_DIR = "preprocess/prep_res"
PRINT_EVERY = 1

NUM_EPOCHS = 1500
DROPOUT = 0.15
INIT_LR = 1e-4
is_local = False

dtype=torch.cuda.FloatTensor
#dtype=torch.FloatTensor

if os.path.exists("../john_local_flag.txt"):
    # this is because my local machine can't handle the batch size...
    is_local = True
    BATCH_SIZE = 4
    NUM_EPOCHS = 1
    dtype = torch.FloatTensor
    NUM_TRAIN = 300
    NUM_VAL = 20
    NUM_SAVED_SAMPLES = 4  # needs to be less than or equal to batch size according to Matt

class ChunkSampler(sampler.Sampler):
    """Samples elements sequentially from some offset. 
    Arguments:
        num_samples: # of desired datapoints
        start: offset where we should start selecting from
    """
    def __init__(self, num_samples, start = 0):
        self.num_samples = num_samples
        self.start = start

    def __iter__(self):
        return iter(range(self.start, self.start + self.num_samples))

    def __len__(self):
        return self.num_samples

def load_dataset():
    ground_truths = []
    inputs = []
    length = len(os.listdir(DATA_DIR))
    if os.path.isfile('saved_in_data.npy') and os.path.isfile('saved_ground_truths.npy'):
        print ("Reading cached numpy data in from file...")
        inputs = np.load('saved_in_data.npy')
        ground_truths = np.load('saved_ground_truths.npy')
        return inputs, ground_truths

    for i, dir in enumerate(os.listdir(DATA_DIR)):
        print ("\tOn dir %d of %d" % (i, length))
        src_p = DATA_DIR + "/" + dir
        if not os.path.isdir(src_p):
            continue
        src_f = sorted(glob.glob(src_p + "/*.jpg"))

        if not all(os.path.exists(f) for f in src_f): continue

        assert (len(src_f) % 3 == 0)

        files = zip(*[iter(src_f)]*3)
        if is_local:
            files = list(files)[:50]

        for zero, truth, one in files:
            t = imread(truth)
            z = imread(zero)
            o = imread(one)
            ground_truths.append(t)
            inputs.append(np.concatenate((z,o), axis=2))

    inputs, ground_truths = np.array(inputs), np.array(ground_truths)

    print ("Caching numpy data for next run...")
    np.save('saved_in_data.npy', inputs)
    np.save('saved_ground_truths.npy', ground_truths)

    return inputs, ground_truths

def make_loaders(inputs, gold):
    inputs_t = torch.from_numpy(inputs).byte()
    gold_t = torch.from_numpy(gold).byte()
    dataset = TensorDataset(inputs_t, gold_t)

    train = DataLoader(dataset, batch_size=BATCH_SIZE, sampler=ChunkSampler(NUM_TRAIN, 0+15500))
    val = DataLoader(dataset, batch_size=BATCH_SIZE, sampler=ChunkSampler(NUM_VAL, NUM_TRAIN+15500))
    test = None # For now

    return train, val, test

def train(model, loss_fn, optimizer, train_data, val_data, num_epochs = 1):
    losses=[]
    eval_losses=[]
    for epoch in range(num_epochs):
        print('Starting epoch %d / %d...' % (epoch + 1, num_epochs))
        model.train()
        for t, (x, y) in enumerate(train_data):
            x_var = Variable(normalize(x).permute(0,3,1,2)).type(dtype)
            y_var = Variable(normalize(y).permute(0,3,1,2)).type(dtype)
            
            scores, oob_loss = model(x_var)
            
            loss = loss_fn(scores, y_var)
            losses.append(loss.data[0])
            #if (t + 1) % PRINT_EVERY == 0:
            print('\ttraining: t = %d, loss = %.4f' % (t + 1, loss.data[0]))
            #if (t) % 50 == 0:
            eval_loss = evaluate(model, val_data, loss_fn)
            eval_losses.append(eval_loss)

            optimizer.zero_grad()
            (loss + oob_loss).backward()
            optimizer.step()
    
    np.save('losses2', np.array(losses))
    np.save('eval_losses2', np.array(eval_losses))

def evaluate(model, dev_data, loss_fn, save=False):
    print("Running evaluation...")
    total_loss = 0.0
    model.eval()
    length = len(dev_data)
    for t, (x, y) in enumerate(dev_data):
        x_var = Variable(normalize(x).permute(0,3,1,2)).type(dtype)
        y_var = Variable(normalize(y).permute(0,3,1,2)).type(dtype)
        
        scores = model(x_var)[0]
        if (t == length-1 and save):
            for i in range(NUM_SAVED_SAMPLES):
                name = "./eval/{}_{}_".format(t, i)
                imsave(name + "gen.png", np.transpose(denorm(scores[i].data.cpu().numpy()), axes=[1,2,0]))
                imsave(name + "gold.png", np.transpose(denorm(y_var[i].data.cpu().numpy()), axes=[1,2,0]))
                x = x_var[i].data.cpu().numpy()
                imsave(name + "orig_0.png", x[:3,:,:])
                imsave(name + "orig_1.png", x[3:,:,:])
        
        total_loss += loss_fn(scores, y_var).data[0]

    print("Total eval loss: %.4f, Avg eval loss: %.4f" % (total_loss, total_loss / NUM_VAL))
    return total_loss


class L2Loss(torch.nn.Module):
    def forward(self, y_pred, y_true):
        diffsq = (y_pred - y_true) **2
        return torch.mean(torch.sum(diffsq.view((-1, 224*224*3)), dim=1))

class TextureLoss(torch.nn.Module):
    """
    Texture Loss is a L2 loss that also penalizes for deltas (textures) over various distances.
    """
    def __init__(self, texture_loss_weight=0.25):
        self.texture_loss_weight = texture_loss_weight

    def forward(self, y_pred, y_true):
        loss = self.l2_loss(y_pred, y_true)
        text_loss = 0
        for i in range(3):
            dist = 2 ** 3
            text_loss += self.l2_loss(self.delta_x(y_pred, dist), self.delta_x(y_true, dist))
            text_loss += self.l2_loss(self.delta_y(y_pred, dist), self.delta_y(y_true, dist))

        return loss + self.texture_loss_weight * text_loss

    @staticmethod
    def delta_x(image, offset):
        return image[:, :, :, :-offset] - image[:, :, :, offset:]

    @staticmethod
    def delta_y(image, offset):
        return image[:, :, :-offset, :] - image[:, :, offset:, :]

    @staticmethod
    def l2_loss(y_pred, y_true):
        N = y_pred.size()[0]
        diffsq = (y_pred - y_true) **2
        return torch.mean(torch.sum(diffsq.view((N, -1)), dim=1))

def run_model(train_data, val_data, test_data):
    model = nn.Sequential (
        EncodeDecode(),
        ViewMorphing()
    ).type(dtype)

    #model = EncodeDecodeDirect().type(dtype)
    
    loss_fn = TextureLoss()
    #optimizer = torch.optim.SGD(model.parameters(), lr=INIT_LR, momentum=0.9) 
    optimizer = optim.Adam(model.parameters(), lr=INIT_LR)

    train(model, loss_fn, optimizer, train_data, val_data, num_epochs=NUM_EPOCHS) 
    #evaluate(model, val_data, loss_fn, save=True)
    evaluate(model, train_data, loss_fn, save=True)

def main():
    print ("Loading dataset...")
    inputs, gold = load_dataset()
    print ("Making loaders...")
    train, val, test = make_loaders(inputs, gold)
    print ("Beginning to run model...")
    run_model(train, val, test)

if __name__ == "__main__":
    main()
