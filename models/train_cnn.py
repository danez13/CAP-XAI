"""Script for training LSTM and CNN models for the e-SNLI dataset."""
import argparse
import random
from functools import partial
from typing import Dict

import numpy as np
import torch
from sklearn.metrics import accuracy_score, confusion_matrix, \
    precision_recall_fscore_support
from torch.optim import AdamW
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data.sampler import BatchSampler
from tqdm import tqdm
from transformers import BertTokenizer

from data_loader import BucketBatchSampler, NLIDataset, collate_nli
from model_builder import CNN_MODEL, EarlyStopping


def train_model(model: torch.nn.Module,
                train_dl: BatchSampler, dev_dl: BatchSampler,
                optimizer: torch.optim.Optimizer,
                scheduler: torch.optim.lr_scheduler.LambdaLR,
                n_epochs: int,
                early_stopping: EarlyStopping) -> (Dict, Dict):
    loss_f = torch.nn.CrossEntropyLoss()

    best_val, best_model_weights = {'val_f1': 0}, None

    for ep in range(n_epochs):
        model.train()
        for batch in tqdm(train_dl, desc='Training'):
            optimizer.zero_grad()
            logits = model(batch[0])
            loss = loss_f(logits, batch[1])
            loss.backward()
            optimizer.step()

        val_p, val_r, val_f1, val_loss, _, _ = eval_model(model, dev_dl)
        current_val = {
            'val_p': val_p, 'val_r': val_r, 'val_f1': val_f1,
            'val_loss': val_loss, 'ep': ep
        }

        print(current_val, flush=True)

        if current_val['val_f1'] > best_val['val_f1']:
            best_val = current_val
            best_model_weights = model.state_dict()

        scheduler.step(val_loss)
        if early_stopping.step(val_f1):
            print('Early stopping...')
            break

    return best_model_weights, best_val


def eval_model(model: torch.nn.Module, test_dl: BucketBatchSampler,
               measure=None):
    model.eval()

    loss_f = torch.nn.CrossEntropyLoss()

    with torch.no_grad():
        labels_all = []
        logits_all = []
        losses = []
        for batch in tqdm(test_dl, desc="Evaluation"):
            logits_val = model(batch[0])
            loss_val = loss_f(logits_val, batch[1])
            losses.append(loss_val.item())

            labels_all += batch[1].detach().cpu().numpy().tolist()
            logits_all += logits_val.detach().cpu().numpy().tolist()

        prediction = np.argmax(np.array(logits_all), axis=-1)

        if measure == 'acc':
            p, r = None, None
            f1 = accuracy_score(labels_all, prediction)
        else:
            p, r, f1, _ = precision_recall_fscore_support(labels_all,
                                                          prediction,
                                                          average='macro')

        print(confusion_matrix(labels_all, prediction))

    return p, r, f1, np.mean(losses), labels_all, prediction

for i in range(1,6):
    dataset_dir = "data/e-SNLI/dataset"
    batch_size = 256
    lr = 0.0001
    patience = 5
    epochs = 100
    seed = 73
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    device = torch.device("cuda")
    tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
    collate_fn = partial(collate_nli, tokenizer=tokenizer, device=device,
                        return_attention_masks=False, pad_to_max_length=False)
    sort_key = lambda x: len(x[0]) + len(x[1])

    model = CNN_MODEL(tokenizer, n_labels=3).to(device)

    print("Loading datasets...")
    train = NLIDataset(dataset_dir, type='train')
    dev = NLIDataset(dataset_dir, type='dev')

    train_dl = BucketBatchSampler(batch_size=batch_size,
                                sort_key=sort_key, dataset=train,
                                collate_fn=collate_fn)
    dev_dl = BucketBatchSampler(batch_size=batch_size,
                            sort_key=sort_key, dataset=dev,
                            collate_fn=collate_fn)

    print(model)
    optimizer = AdamW(model.parameters(), lr=lr)
    scheduler = ReduceLROnPlateau(optimizer)
    es = EarlyStopping(patience=patience, percentage=False, mode='max',
                    min_delta=0.0)

    best_model_w, best_perf = train_model(model, train_dl, dev_dl, optimizer, scheduler, epochs, es)

    checkpoint = {
    'performance': best_perf,
    'batch size': batch_size, 
    'learning rate': lr, 
    'patience': patience, 
    'epochs': epochs,
    'model': best_model_w,
    }
    print(best_perf)
    print(f"{batch_size}, {lr}, {patience}, {epochs}")

    torch.save(checkpoint, f"data/models/snli/cnn/cnn_{i}")
