import math

from Bio import SeqIO
import edlib
import mappy as mp
import torch
import torch.nn as nn

import graph_parser
from hyperparameters import get_hyperparameters
import models


def anchor(reads, current, aligner):
    sequence = reads[current]
    alignment = aligner.map(sequence)
    hit = list(alignment)[0]
    r_st, r_en, strand = hit.r_st, hit.r_en, hit.strand
    return r_st, r_en, strand


def get_overlap_length(graph, reads, current, neighbor):
    idx = graph_parser.find_edge_index(graph, current, neighbor)
    overlap_length = len(reads[current]) - graph.prefix_length[idx]
    return overlap_length


def get_suffix(reads, node, overlap_length):
    return reads[node][overlap_length:]


def get_paths(start, neighbors, num_nodes):
    if num_nodes == 0:
        return [[start]]
    paths = []
    for neighbor in neighbors[start]:
        next_paths = get_paths(neighbor, neighbors, num_nodes-1)
        for path in next_paths:
            path.append(start)
            paths.append(path)
    return paths


def get_edlib_best(idx, graph, reads, current, neighbors, reference_seq, aligner, visited):
    ref_start, ref_end, strand = anchor(reads, current, aligner)
    edlib_start = ref_start
    paths = [path[::-1] for path in get_paths(current, neighbors, num_nodes=4)]
    distances = []
    for path in paths:
        _, _, next_strand = anchor(reads, path[1], aligner)
        if next_strand != strand:
            continue
        sequence = graph_parser.translate_nodes_into_sequence2(graph, reads, path[1:])
        if strand == -1:
            sequence = sequence.reverse_complement()
        edlib_start = ref_start + graph.edata['prefix_length'][graph_parser.find_edge_index(graph, path[0], path[1])].item()
        edlib_end = edlib_start + len(sequence)
        reference_query = reference_seq[edlib_start:edlib_end]
        distance = edlib.align(reference_query, sequence)['editDistance']
        score = distance / (edlib_end - edlib_start)
        distances.append((path, score))
    try:
        best_path, min_distance = min(distances, key=lambda x: x[1])
        best_neighbor = best_path[1]
        return best_neighbor
    except ValueError:
        print('\nAll the next neighbors have an opposite strand')
        print('Graph index:', idx)
        print('current:,', current)
        print(paths)
        return None
        

def get_minimap_best(graph, reads, current, neighbors, walk, aligner):
    scores = []
    for neighbor in neighbors[current]:
        print(f'\tcurrent neighbor {neighbor}')
        node_tr = walk[-min(3, len(walk)):] + [neighbor]
        sequence = graph_parser.translate_nodes_into_sequence2(graph, reads, node_tr)
        ll = min(len(sequence), 50000)
        sequence = sequence[-ll:]
        name = '_'.join(map(str, node_tr)) + '.fasta'
        with open(f'concat_reads/{name}', 'w') as fasta:
            fasta.write(f'>{name}\n')
            fasta.write(f'{str(sequence)*10}\n')
        alignment = aligner.map(sequence)
        hits = list(alignment)
        try:
            quality_score = graph_parser.get_quality(hits, len(sequence))
        except:
            quality_score = 0
        print(f'\t\tquality score:', quality_score)
        scores.append((neighbor, quality_score))
    best_neighbor, quality_score = max(scores, key=lambda x: x[1])
    return best_neighbor


def print_prediction(walk, current, neighbors, actions, choice, best_neighbor):
    print('\n-----predicting-----')
    print('previous:\t', None if len(walk) < 2 else walk[-2])
    print('current:\t', current)
    print('neighbors:\t', neighbors[current])
    print('actions:\t', actions.tolist())
    print('choice:\t\t', choice)
    print('ground truth:\t', best_neighbor)


def process(model, idx, graph, pred, neighbors, reads, reference, optimizer, mode, device='cpu'):
    hyperparameters = get_hyperparameters()
    dim_latent = hyperparameters['dim_latent']
    last_latent = torch.zeros((graph.num_nodes(), dim_latent)).to(device).detach()
    start_nodes = list(set(range(graph.num_nodes())) - set(pred.keys()))
    start = start_nodes[0]  # TODO: Maybe iterate over all the start nodes?

    criterion = nn.CrossEntropyLoss()
    aligner = mp.Aligner(reference, preset='map_pb', best_n=1)
    reference_seq = next(SeqIO.parse(reference, 'fasta'))

    current = start
    visited = set()
    walk = []
    loss_list = []
    total_loss = 0
    total = 0
    correct = 0
    print('Iterating through nodes!')

    # Embed the graph with GCN model
    if isinstance(model, models.GCNModel):
        last_latent = model(graph, last_latent, device, 'embed')
        predict_actions = model(graph, last_latent, device, 'classify')
        print(predict_actions.shape)

    while True:
        walk.append(current)
        if current in visited:
            break
        visited.add(current)  # current node
        visited.add(current ^ 1)  # virtual pair of the current node
        try:
            if len(neighbors[current]) == 0:
                break
        except KeyError:
            print(current)
            raise
        if len(neighbors[current]) == 1:
            current = neighbors[current][0]
            continue

        # Currently not used, but could be used for calculating loss
        mask = torch.tensor([1 if n in neighbors[current] else -math.inf for n in range(graph.num_nodes())]).to(device)

        # Get prediction for the next node out of those in list of neighbors (run the model)
        if isinstance(model, models.SequentialModel):
            predict_actions, last_latent = model(graph, latent_features=last_latent, device=device)
        actions = predict_actions.squeeze(1)[neighbors[current]]
        value, index = torch.topk(actions, k=1, dim=0)  # For evaluation
        choice = neighbors[current][index]

        # Branching found - find the best neighbor with edlib
        best_neighbor = get_edlib_best(idx, graph, reads, current, neighbors, reference_seq, aligner, visited)
        print_prediction(walk, current, neighbors, actions, choice, best_neighbor)

        if best_neighbor is None:
            break

        # Calculate loss
        # TODO: Modify for batch_size > 1
        loss = criterion(actions.unsqueeze(0), index.to(device))
        total_loss += loss
        loss_list.append(loss.item())

        if choice == best_neighbor:
            correct += 1
        total += 1

        # Teacher forcing
        current = best_neighbor

        # Update weights for sequential model
        if isinstance(model, models.SequentialModel) and mode == 'train':
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

    # Update weights for non-sequential model
    if isinstance(model, models.GCNModel) and mode == 'train':
        optimizer.zero_grad()
        total_loss.backward()
        optimizer.step()

    accuracy = correct / total
    return loss_list, accuracy