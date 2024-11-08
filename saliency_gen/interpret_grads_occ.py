"""Script to serialize the saliency with gradient approaches and occlusion."""
import argparse
import json
import os
import random
from argparse import Namespace
from collections import defaultdict
from functools import partial

import numpy as np
import torch
from captum.attr import DeepLift, GuidedBackprop, InputXGradient, Occlusion, \
    Saliency, configure_interpretable_embedding_layer, \
    remove_interpretable_embedding_layer
from torch.utils.data import DataLoader
from tqdm import tqdm
from transformers import BertTokenizer

from models.data_loader import collate_nli, NLIDataset
from models.model_builder import CNN_MODEL


def summarize_attributions(attributions, type='mean', model=None, tokens=None):
    if type == 'none':
        return attributions
    elif type == 'dot':
        embeddings = get_model_embedding_emb(model)(tokens)
        attributions = torch.einsum('bwd, bwd->bw', attributions, embeddings)
    elif type == 'mean':
        attributions = attributions.mean(dim=-1).squeeze(0)
        attributions = attributions / torch.norm(attributions)
    elif type == 'l2':
        attributions = attributions.norm(p=1, dim=-1).squeeze(0)
    return attributions


class BertModelWrapper(torch.nn.Module):
    def __init__(self, model):
        super(BertModelWrapper, self).__init__()
        self.model = model

    def forward(self, input, attention_mask, labels):
        return self.model(input, attention_mask=attention_mask)[0]


def get_model_embedding_emb(model):
    return model.embedding.embedding


def generate_saliency(model_path, saliency_path, saliency, aggregation, dataset_dir, bs, sw):
    checkpoint = torch.load(model_path,
                            map_location=lambda storage, loc: storage)
    model_args = Namespace(**checkpoint['args'])

    model = CNN_MODEL(tokenizer, model_args, n_labels=checkpoint['args']['labels']).to(device)
    model.load_state_dict(checkpoint['model'])

    model.train()

    pad_to_max = False
    if saliency == 'deeplift':
        ablator = DeepLift(model)
    elif saliency == 'guided':
        ablator = GuidedBackprop(model)
    elif saliency == 'sal':
        ablator = Saliency(model)
    elif saliency == 'inputx':
        ablator = InputXGradient(model)
    elif saliency == 'occlusion':
        ablator = Occlusion(model)

    collate_fn = partial(collate_nli, tokenizer=tokenizer, device=device,
                         return_attention_masks=False,
                         pad_to_max_length=pad_to_max)
    test = NLIDataset(dataset_dir, type="test", salient_features=True)
    batch_size = bs if bs != None else \
        model_args.batch_size
    test_dl = DataLoader(batch_size=batch_size, dataset=test, shuffle=False,
                         collate_fn=collate_fn)

    # PREDICTIONS
    predictions_path = model_path + '.predictions'
    if not os.path.exists(predictions_path):
        predictions = defaultdict(lambda: [])
        for batch in tqdm(test_dl, desc='Running test prediction... '):
            logits = model(batch[0])
            logits = logits.detach().cpu().numpy().tolist()
            predicted = np.argmax(np.array(logits), axis=-1)
            predictions['class'] += predicted.tolist()
            predictions['logits'] += logits

        with open(predictions_path, 'w') as out:
            json.dump(predictions, out)

    # COMPUTE SALIENCY
    if saliency != 'occlusion':
        embedding_layer_name = 'embedding'
        interpretable_embedding = configure_interpretable_embedding_layer(model,
                                                                          embedding_layer_name)

    class_attr_list = defaultdict(lambda: [])
    token_ids = []
    saliency_flops = []

    for batch in tqdm(test_dl, desc='Running Saliency Generation...'):
        additional = None

        token_ids += batch[0].detach().cpu().numpy().tolist()
        if saliency != 'occlusion':
            input_embeddings = interpretable_embedding.indices_to_embeddings(
                batch[0])
            
        for cls_ in range(checkpoint['args']['labels']):
            if saliency == 'occlusion':
                attributions = ablator.attribute(batch[0],
                                                 sliding_window_shapes=(
                                                 sw,), target=cls_,
                                                 additional_forward_args=additional)
            else:
                attributions = ablator.attribute(input_embeddings, target=cls_,
                                                 additional_forward_args=additional)

            attributions = summarize_attributions(attributions,
                                                  type=aggregation, model=model,
                                                  tokens=batch[
                                                      0]).detach().cpu(

            ).numpy().tolist()
            class_attr_list[cls_] += [[_li for _li in _l] for _l in
                                      attributions]

    if saliency != 'occlusion':
        remove_interpretable_embedding_layer(model, interpretable_embedding)

    # SERIALIZE
    print('Serializing...', flush=True)
    with open(saliency_path, 'w') as out:
        for instance_i, _ in enumerate(test):
            saliencies = []
            for token_i, token_id in enumerate(token_ids[instance_i]):
                token_sal = {'token': tokenizer.ids_to_tokens[token_id]}
                for cls_ in range(checkpoint['args']['labels']):
                    token_sal[int(cls_)] = class_attr_list[cls_][instance_i][
                        token_i]
                saliencies.append(token_sal)

            out.write(json.dumps({'tokens': saliencies}) + '\n')
            out.flush()

    return saliency_flops

seed = 73
saliency = ["guided","sal","inputx","occlusion"]
model_dir = "data/models/snli/cnn/cnn"
output_dir = "data\saliency\snli\cnn"
dataset_dir = "data/e-SNLI/dataset"
batch_size = None
sliding_window = 1
random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
torch.backends.cudnn.deterministic = True
np.random.seed(seed)

device = torch.device("cuda")

tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

for saliency in saliency:
    print('Running Saliency ', saliency, flush=True)

    if saliency in ['guided', 'sal', 'inputx', 'deeplift']:
        aggregations = ['mean', 'l2']  #
    else:  # occlusion
        aggregations = ['none']

    for aggregation in aggregations:
        flops = []
        print('Running aggregation ', aggregation, flush=True)

        models_dir = models_dir
        base_model_name = models_dir.split('/')[-1]
        for model in range(1, 6):
            curr_flops = generate_saliency(
                os.path.join(models_dir + f'_{model}'),
                os.path.join(output_dir, f'{base_model_name}_{model}_{saliency}_{aggregation}'), 
                saliency, 
                aggregation, 
                dataset_dir, 
                batch_size, 
                sliding_window)

            flops.append(np.average(curr_flops))

        print('FLOPS', np.average(flops), np.std(flops), flush=True)
        print()
        print()
