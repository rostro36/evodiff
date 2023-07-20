from evodiff.model import ByteNetLMTime, TransformerTime
import numpy as np
import argparse
from sequence_models.constants import MSA_ALPHABET, ALL_AAS, PROTEIN_ALPHABET, PAD
import torch
import os
import glob
import json
from evodiff.utils import Tokenizer
import pathlib
from sequence_models.datasets import UniRefDataset
from tqdm import tqdm
from evodiff.plot import aa_reconstruction_parity_plot
import pandas as pd
import random

home = str(pathlib.Path.home())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config_fpath')
    parser.add_argument('out_fpath', type=str, nargs='?', default=os.getenv('PT_OUTPUT_DIR', '/tmp') + '/')
    parser.add_argument('--dropout', type=float, default=0.0)
    parser.add_argument('--tie_weights', action='store_true')
    parser.add_argument('--final_norm', action='store_true')
    parser.add_argument('--norm_first', action='store_true') # turns norm_first on in transformer model
    parser.add_argument('--checkpoint', type=int, default=None)
    parser.add_argument('--num-seqs', type=int, default=20)
    parser.add_argument('--mask', type=str, default='autoreg')
    parser.add_argument('--penalty', type=float, default=None) # repetition penalty, commonly 1.2 is used
    parser.add_argument('--model_type', type=str, default='ByteNet',
                        help='ByteNet or Transformer')
    parser.add_argument('-g', '--gpus', default=1, type=int,
                        help='number of gpus per node')
    parser.add_argument('--no-step', action='store_true') # For D3PM if true will predict x_0 from x_t, instead of x_tminus1
    parser.add_argument('--delete-prev',action='store_true')  # Will delete previous generated sequences
    parser.add_argument('--count', default=0, type=int) # Start new gen sequences from 0, unless
    #parser.add_argument('--idr',action='store_true')  # Will delete previous generated sequences
    args = parser.parse_args()

    _ = torch.manual_seed(0)
    np.random.seed(0)

    data = UniRefDataset('data/uniref50/', 'train', structure=False, max_len=2048)

    with open(args.config_fpath, 'r') as f:
        config = json.load(f)

    d_embed = config['d_embed']
    d_model = config['d_model']
    n_layers = config['n_layers']
    if args.model_type == 'Transformer':
        n_head = config['n_head']
        d_feedforward = config['d_feedforward']
    if args.model_type == 'ByteNet':
        kernel_size = config['kernel_size']
        r = config['r']
    if 'rank' in config:
        weight_rank = config['rank']
    else:
        weight_rank = None
    if 'slim' in config:
        slim = config['slim']
    else:
        slim = True
    if 'activation' in config:
        activation = config['activation']
    else:
        activation = 'relu'
    data_top_dir = home + '/Desktop/DMs/data/'
    project_dir = home + '/Desktop/DMs/'

    torch.cuda.set_device(args.gpus)
    device = torch.device('cuda:' + str(args.gpus))
    #idr_flag = ''
    causal = False
    bidirectional = True
    n_tokens = len(MSA_ALPHABET)

    if args.mask == 'autoreg' or args.mask == 'so' or args.mask == 'reference' or args.mask == 'test-sample' or args.mask == 'bert':
        tokenizer = Tokenizer()
        diffusion_timesteps = None  # Not input to model
        if args.mask == 'so' or args.mask == 'bert':
            n_tokens = len(PROTEIN_ALPHABET)
            tokenizer = Tokenizer(protein_alphabet=PROTEIN_ALPHABET, all_aas=ALL_AAS, pad=PAD)
            if args.mask == 'so':
                causal = True
                bidirectional = False
        # if args.idr:
        #     idr_flag = 'idr_'
        #     print("IDR GENERATION ONLY WORKS WITH args.mask = 'autoreg' OR 'so'")
    elif args.mask == 'blosum' or args.mask == 'random':
        tokenizer = Tokenizer(path_to_blosum=data_top_dir + "blosum62-special-MSA.mat", sequences=True)
        diffusion_timesteps = config['diffusion_timesteps']
        if args.mask == 'random':
            Q_prod, Q_t = tokenizer.q_random_schedule(timesteps=diffusion_timesteps)
        if args.mask == 'blosum':
            Q_prod, Q_t = tokenizer.q_blosum_schedule(timesteps=diffusion_timesteps)
        Q_prod = Q_prod.to(device)
        Q_t = Q_t.to(device)
    else:
        print("Choose 'autoreg', 'so', 'test-sample', 'reference', 'blosum' or 'random' as args.mask OR chose idr")
    print("Using", args.mask, "scheme")
    masking_idx = tokenizer.mask_id
    padding_idx = tokenizer.pad_id
    print(n_tokens)
    print(masking_idx, padding_idx)
    print("causal", causal)

    if args.model_type == 'ByteNet':
        model = ByteNetLMTime(n_tokens, d_embed, d_model, n_layers, kernel_size, r,
                          causal=causal, padding_idx=masking_idx, rank=weight_rank, dropout=args.dropout,
                          tie_weights=args.tie_weights, final_ln=args.final_norm, slim=slim, activation=activation,
                          timesteps=diffusion_timesteps)
    elif args.model_type == 'Transformer':
        model = TransformerTime(n_tokens, d_embed, d_model, n_layers, n_head, d_feedforward, padding_idx=masking_idx,
                                bidirectional=bidirectional, dropout=args.dropout,
                                norm_first=args.norm_first, activation=activation, timesteps=diffusion_timesteps)
    model = model.to(device)

    if args.checkpoint is not None:
        last_epoch = args.checkpoint
    else:
        # Restore the model weights for the last checkpoint after training
        outputs = os.listdir(args.out_fpath)
        if len(outputs) > 0:
           last_epoch = 0
           for output in outputs:
               if 'checkpoint' in output:
                   epoch = int(output.split('checkpoint')[-1][:-4])
                   if epoch > last_epoch:
                       args.state_dict = args.out_fpath + output
                       last_epoch = epoch

    if args.mask != 'reference' and args.mask != 'test-sample':
        print('Using checkpoint', last_epoch)
        print('Loading weights from ' + args.state_dict + '...')
        sd = torch.load(args.state_dict, map_location=torch.device(device))
        msd = sd['model_state_dict']
        msd = {k.split('module.')[1]: v for k, v in msd.items()}
        model.load_state_dict(msd)

    #seq_lengths = [64, 128, 256, 384] #, 64, 128, 256, 384, 512] #, 1024, 2048] # Generate diff length sequences
    seq_lengths = pd.read_csv('count/seq_len10000.csv')
    seqs = ""
    seqs_only = ""
    overall_count = 0

    if args.delete_prev:
        filelist = glob.glob(args.out_fpath+'generated*')
        for file in filelist:
            os.remove(file)
            print("Deleting", file)

    if args.mask != 'test-sample' and args.mask !='so':
        for i in tqdm(range(args.num_seqs)):
            r_idx = np.random.choice(len(data))
            seq_len = len(data[r_idx][0]) # randomly sample a length from train data
            #print(seq_len)
            #seq_len = int(random.sample(list(seq_lengths), 1)[0])
            #print(seq_len)
            #with open(args.out_fpath + 'generated_samples_string_' + str(seq_len) + '.fasta', 'a') as f:
            count = args.count
            fasta_string = ""

            if args.mask == 'autoreg' or args.mask == 'bert':
                sample, string = generate_oaardm(model, seq_len, tokenizer=tokenizer, penalty=args.penalty,
                                               batch_size=1, device=device)
            elif args.mask == 'blosum' or args.mask == 'random':
                sample, string = generate_d3pm(model, seq_len, Q_bar=Q_prod, Q=Q_t, tokenizer=tokenizer,
                                                    timesteps=diffusion_timesteps, no_step=args.no_step,
                                                    batch_size=args.num_seqs, device=device,
                                                    model_type=args.model_type)
            elif args.mask == 'reference':
                sample = []
                string = []
                train_prob_dist = aa_reconstruction_parity_plot(project_dir, args.out_fpath, 'placeholder.csv', gen_file=False)
                for j in range(args.num_seqs):
                    print(j)
                    _sample, _string = generate_random_seq(seq_len, train_prob_dist, tokenizer=tokenizer)
                    sample.append(_sample)
                    string.append(_string)

            for _s in string:
                fasta_string += ">SEQUENCE_" + str(count) + "\n" + str(_s) + "\n"
                count += 1
                seqs += ">SEQUENCE_" + str(overall_count) + "\n" + str(_s) + "\n"
                overall_count += 1
                seqs_only += str(_s) + "\n"

            # f.write(fasta_string)
            # f.close()

        with open(args.out_fpath + 'generated_samples_string.fasta', 'a') as f:
            f.write(seqs)
            f.write('\n')

        with open(args.out_fpath + 'generated_samples_string.csv', 'a') as f:
            f.write(''.join([seqs_only]))
            f.write('\n')


    elif (args.mask == 'test-sample' or args.mask == 'so'):
        overall_count = 0
        seqs = ""
        if args.mask == 'test-sample':
            string = generate_valid_subset(data_top_dir=data_top_dir, samples=args.num_seqs)
            # for i, seq_len in enumerate(seq_lengths):
            #     with open(args.out_fpath + 'generated_samples_string_' + str(seq_len) + '.fasta', 'a') as f:
            #         fasta_string = ""
            #         count = args.count
            #         print(len(string[i]))
            #         for _s in string[i]:
            #             fasta_string += ">SEQUENCE_" + str(count) + "\n" + str(_s) + "\n"
            #             count += 1
            #             seqs += ">SEQUENCE_" + str(overall_count) + "\n" + str(_s) + "\n"
            #             overall_count += 1
            #         f.write(fasta_string)
            #         f.close()
        #     with open(args.out_fpath + 'generated_samples_string.fasta', 'a') as f:
        #         f.write(seqs)
        #         f.write('\n')
        elif args.mask == 'so':
            #seq_len=100 # placeholder
            sample, string = generate_autoreg(model, samples=args.num_seqs, tokenizer=tokenizer, penalty=args.penalty,
                                              batch_size=args.num_seqs, device=device)
        with open(args.out_fpath + 'generated_samples_string.csv', 'a') as f:
            f.write(''.join([_s + "\n" for _s in string]))
        with open(args.out_fpath + 'generated_samples_string.fasta', 'a') as f:
            f.write(''.join([">SEQUENCE_" + str(i) + "\n" + str(_s) + "\n" for i,_s in enumerate(string)]))
    # elif args.idr:
    #     sample, string, queries, sequences = generate_idr(model, data_top_dir, tokenizer=tokenizer, penalty=args.penalty,
    #                                   causal=causal, batch_size=args.num_seqs, device=device)
    #     print(queries)
    #     seqs_old=""
    #     seqs_old_only=""
    #     for i, _s in enumerate(string):
    #         seqs += ">GEN_" + queries[i] + "\n" + str(_s) + "\n"
    #         seqs_old +=  ">" + queries[i] + "\n" + str(sequences[i]) + "\n"
    #         seqs_only += str(_s) + "\n"
    #         seqs_old_only += str(sequences[i]) + "\n"
    #     with open(args.out_fpath + 'generated_idr.fasta', 'a') as f:
    #         f.write(seqs)
    #         f.write('\n')
    #     with open(args.out_fpath + 'data_idr.fasta', 'a') as f:
    #         f.write(seqs_old)
    #         f.write('\n')
    #     with open(args.out_fpath + 'data_idr.csv', 'a') as f:
    #         f.write(seqs_old_only)
    #     with open(args.out_fpath + idr_flag +'generated_samples_string.csv', 'a') as f:
    #         f.write(''.join([seqs_only]))
    #         f.write('\n')


    # Plot distribution of generated samples
    if args.mask != 'test-sample':
        aa_reconstruction_parity_plot(project_dir, args.out_fpath, 'generated_samples_string.csv')

# def get_IDR_sequences(data_top_dir, tokenizer):
#     sequences = []
#     masked_sequences = []
#     start_idxs = []
#     end_idxs = []
#     queries = []
#     # GET IDRS
#     data_dir = data_top_dir + 'human_idr_alignments/'
#     all_files = os.listdir(data_dir + 'human_protein_alignments')
#     index_file = pd.read_csv(data_dir + 'human_idr_boundaries.tsv', delimiter='\t')
#     print(len(index_file), "TOTAL IDRS")
#     for index, row in index_file[:50].iterrows(): # TODO only iterating over 100 right now
#         msa_file = [file for i, file in enumerate(all_files) if row['OMA_ID'] in file][0]
#         msa_data, msa_names = parse_fasta(data_dir + 'human_protein_alignments/' + msa_file, return_names=True)
#         query_idx = [i for i, name in enumerate(msa_names) if name == row['OMA_ID']][0]  # get query index
#         queries.append(row['OMA_ID'])
#         # JUST FOR SEQUENCES
#         #print("IDR:\n", row['IDR_SEQ'])
#         #print("MSA IDR NO GAPS:\n", msa_data[query_idx].replace("-", ""))
#         seq_only = msa_data[query_idx].replace("-", "")
#         sequences.append(seq_only)
#         start_idx = row['START'] - 1
#         end_idx = row['END']
#         idr_range = end_idx - start_idx
#         #print(start_idx, end_idx, idr_range)
#         masked_sequence = seq_only[0:start_idx] + '#' * idr_range + seq_only[end_idx:]
#         #print("MASKED SEQUENCE:\n", masked_sequence)
#         masked_sequences.append(masked_sequence)
#         start_idxs.append(start_idx)
#         end_idxs.append(end_idx)
#     tokenized = [torch.tensor(tokenizer.tokenizeMSA(s)) for s in masked_sequences]
#     #print(tokenized[0])
#     return tokenized, start_idxs, end_idxs, queries, sequences

def unravel_index(index, shape):
    out = []
    for dim in reversed(shape):
        out.append(index % dim)
        index = index // dim
    return list(reversed(out))


def generate_oaardm(model, seq_len, tokenizer=Tokenizer(), penalty=None, batch_size=20, device='cuda'):
    # Generate a random start string and convert to tokens
    all_aas = tokenizer.all_aas
    mask = tokenizer.mask_id
    # Start from mask
    seq_len=100
    sample = torch.zeros((batch_size, seq_len))+mask
    sample = sample.to(torch.long)
    sample = sample.to(device)
    # Unmask 1 loc at a time randomly
    # TODO need to shuffle order for every sequence in the batch
    loc = np.arange(seq_len)
    #np.random.shuffle(loc)
    timestep = torch.tensor([0] * batch_size)  # placeholder but not called in model
    timestep = timestep.to(device)
    residues_appended = []
    with torch.no_grad():
        for i in loc:
            # Prob-based loc sampling
            prediction = model(sample, timestep) #, input_mask=input_mask.unsqueeze(-1)) #sample prediction given input
            mask_loc = (sample[0] == mask).to(float)
            print("mask", int(mask_loc.sum().item()), "of", len(sample[0]))
            p = torch.nn.functional.softmax(prediction[:, :, :len(all_aas)-6], dim=1)
            mask_loc_exp = mask_loc.unsqueeze(-1).unsqueeze(0).expand(-1, -1,len(all_aas)-6) # set non-mask p to 0
            sampled_loc = torch.argmax(p*mask_loc_exp, dim=2)
            print(sampled_loc)
            max_loc, p_sample = torch.max(sampled_loc, dim=-1)
            #print(p_sample)
            if i == 0:
                max_loc = torch.randint(0, seq_len, (1,))
                print(max_loc)
            p_sample = torch.multinomial(p[0, max_loc], num_samples=1)
            #print(p_sample)
            #import pdb; pdb.set_trace()
            # new_max = torch.multinomial(location, num_samples=1, replacement=True)
            # #print(new_max[0].item())
            # new_b, new_l, new_w = unravel_index(new_max[0].item(), (b, l, w))
            # p_sample = new_w
            sample[:, max_loc.item()] = p_sample.item()
            print([tokenizer.untokenize(s) for s in sample])  # check that sampling correctly

            # Below uses random loc sampling
            # p = prediction[:, i, :len(all_aas)-6] # sample at location i (random), dont let it predict non-standard AA
            # p = torch.nn.functional.softmax(p, dim=1) # softmax over categorical probs
            # p_sample = torch.multinomial(p, num_samples=1)
            # # Repetition penalty
            # if penalty is not None: # ignore if value is None
            #     for j in range(batch_size): # iterate over each obj in batch
            #         case1 = (i == 0 and sample[j, i+1] == p_sample[j]) # beginning of seq
            #         case2 = (i == seq_len-1 and sample[j, i-1] == p_sample[j]) # end of seq
            #         case3 = ((i < seq_len-1 and i > 0) and ((sample[j, i-1] == p_sample[j]) or (sample[j, i+1] == p_sample[j]))) # middle of seq
            #         if case1 or case2 or case3:
            #             #print("identified repeat", p_sample, sample[i-1], sample[i+1])
            #             p[j, int(p_sample[j])] /= penalty # reduce prob of that token by penalty value
            #             p_sample[j] = torch.multinomial(p[j], num_samples=1) # resample
            # sample[:, i] = p_sample.squeeze()
            # #print([tokenizer.untokenize(s) for s in sample]) # check that sampling correctly

    #print("final seq", [tokenizer.untokenize(s) for s in sample])
    untokenized = [tokenizer.untokenize(s) for s in sample]
    return sample, untokenized

def generate_autoreg(model, samples=100, tokenizer=Tokenizer(), penalty=None, batch_size=1, device='cuda',
                     max_seq_len=500):
    # Generates 1 seq at a time, no batching, to make it easier to deal w variable seq lengths
    # Generates until max length or until
    start = tokenizer.start_id
    stop = tokenizer.stop_id
    sample_out = []
    untokenized_out = []
    timestep = torch.tensor([0] * batch_size)  # placeholder but not called in model
    timestep = timestep.to(device)
    for s in tqdm(range(samples)):
        # Start from START token
        sample = (torch.zeros((1))+ start).unsqueeze(0) # add batch dim
        sample = sample.to(torch.long)
        sample = sample.to(device)
        # Iterate over each residue until desired length
        #max_loc = np.arange(max_seq_len)
        reach_stop=False # initialize
        with torch.no_grad():
            for i in range(max_seq_len):
                if reach_stop == False: # Add residues until it predicts STOP token or hits max seq len
                    prediction = model(sample, timestep) #, input_mask=input_mask.unsqueeze(-1)) #sample prediction given input
                    p = prediction[:, -1, :] # predict next token
                    p = torch.nn.functional.softmax(p, dim=1) # softmax over categorical probs
                    p_sample = torch.multinomial(p, num_samples=1)
                    sample = torch.cat((sample, p_sample), dim=1)
                    #print(tokenizer.untokenize(sample[0]))
                    #print(p_sample, stop)
                    if p_sample == stop:
                        reach_stop = True
                else:
                    break

        print("final seq", tokenizer.untokenize(sample[0,1:-1])) # dont care about appending start/stop token
        untokenized = tokenizer.untokenize(sample[0,1:-1])
        sample_out.append(sample[0,1:-1])
        untokenized_out.append(untokenized)
    return sample_out, untokenized_out

def generate_idr(model, data_top_dir, tokenizer=Tokenizer(), penalty=None, causal=False, batch_size=20, device='cuda'):
    cutoff = 256 # TODO ADD FILTER

    all_aas = tokenizer.all_aas
    tokenized_sequences, start_idxs, end_idxs, queries, sequences = get_IDR_sequences(data_top_dir, tokenizer)
    samples = []
    samples_idr = []
    sequences_idr = []
    # Manually batch IDR sequences jk cant batch bc all IDRs have diff masking regions
    #batches = math.ceil(len(tokenized_sequences)/batch_size)
    for s, sample in enumerate(tokenized_sequences):
        loc = np.arange(start_idxs[s], end_idxs[s])
        if len(loc) < cutoff:
            print("QUERY", queries[s])
            #print("ORIGINAL SEQUENCE", sequences[s])
            print("ORIGINAL IDR", sequences[s][start_idxs[s]:end_idxs[s]])
            sequences_idr.append(sequences[s][start_idxs[s]:end_idxs[s]])
            sample = sample.to(torch.long)
            sample = sample.to(device)
            seq_len = len(sample)
            #print(start_idxs[s], end_idxs[s])
            if causal == False:
                np.random.shuffle(loc)
            with torch.no_grad():
                for i in tqdm(loc):
                    timestep = torch.tensor([0]) # placeholder but not called in model
                    timestep = timestep.to(device)
                    prediction = model(sample.unsqueeze(0), timestep) #, input_mask=input_mask.unsqueeze(-1)) #sample prediction given input
                    p = prediction[:, i, :len(all_aas)-6] # sample at location i (random), dont let it predict non-standard AA
                    p = torch.nn.functional.softmax(p, dim=1) # softmax over categorical probs
                    p_sample = torch.multinomial(p, num_samples=1)
                    # Repetition penalty
                    if penalty is not None: # ignore if value is None
                        for j in range(batch_size): # iterate over each obj in batch
                            case1 = (i == 0 and sample[j, i+1] == p_sample[j]) # beginning of seq
                            case2 = (i == seq_len-1 and sample[j, i-1] == p_sample[j]) # end of seq
                            case3 = ((i < seq_len-1 and i > 0) and ((sample[j, i-1] == p_sample[j]) or (sample[j, i+1] == p_sample[j]))) # middle of seq
                            if case1 or case2 or case3:
                                p[j, int(p_sample[j])] /= penalty # reduce prob of that token by penalty value
                                p_sample[j] = torch.multinomial(p[j], num_samples=1) # resample
                    sample[i] = p_sample.squeeze()
                    #print(tokenizer.untokenize(sample))
            #print("GENERATED SEQUENCES", tokenizer.untokenize(sample))
            print("GENERATED IDR", tokenizer.untokenize(sample[start_idxs[s]:end_idxs[s]]))
            samples.append(sample)
            samples_idr.append(sample[start_idxs[s]:end_idxs[s]])
        else:
            pass
    untokenized = [tokenizer.untokenize(s) for s in samples]
    untokenized_idr = [tokenizer.untokenize(s) for s in samples_idr]
    return samples, untokenized_idr, queries, sequences_idr

def generate_d3pm(model, seq_len, Q_bar=None, Q=None, tokenizer=Tokenizer(), timesteps=500, no_step=False,
                       batch_size=20, device='cuda', model_type='ByteNet'):
    """
    no_step: if true will calculate p_tilde(x_0|x_t) from a uniform sample
             if false will calculate p_tilde(x_tminus1|x_t) for each t in timestep
    """
    # Generate a random start string from uniform dist and convert to tokens
    #all_aas = tokenizer.all_aas

    sample = torch.randint(0, tokenizer.K, (batch_size, seq_len)) # don't include gap token?
    sample = sample.to(torch.long)
    sample = sample.to(device)
    sample_og = sample
    #print("input seq", tokenizer.untokenize(sample))
    if no_step:
        timesteps = np.linspace(timesteps-1, timesteps-1, 1, dtype=int)
    else:
        timesteps = np.linspace(timesteps-1,1,int((timesteps-1)/1), dtype=int) # iterate over reverse timesteps
    with torch.no_grad():
        for t in tqdm(timesteps):
            timesteps = torch.tensor([t] * batch_size)
            timesteps = timesteps.to(device)
            #prediction = model(sample, timesteps)
            if model_type == 'ByteNet':
                prediction = model(sample, timesteps)
            elif model_type == 'Transformer':
                prediction = model(sample, sample, timesteps) # TODO fix target?
            print(prediction)
            p = prediction[:, :, :tokenizer.K]  # p_theta_tilde (x_0_tilde | x_t) # Don't predict non-standard AAs
            p = torch.nn.functional.softmax(p, dim=-1)  # softmax over categorical probs
            p = p.to(torch.float64)
            #print(p)
            if no_step: # This one-step model should give you a bad distribution if conditioned properly
                x_tminus1 = sample.clone()
                for i in range(len(p)):
                    x_tminus1[i] = torch.multinomial(p[i], num_samples=1).squeeze()
            else:
                x_tminus1 = sample.clone()
                for i, s in enumerate(sample):
                    #print("starting sequence", tokenizer.untokenize(s))
                    # Calculate p_theta_marg from p_theta_tilde
                    x_t_b = tokenizer.one_hot(s)
                    A = torch.mm(x_t_b, torch.t(Q[t]))  # [P x K]
                    Q_expand = Q_bar[t-1].unsqueeze(0).expand(A.shape[0], tokenizer.K, tokenizer.K)  # [ P x K x K]
                    B_pred = torch.mul(p[i].unsqueeze(2), Q_expand)
                    q_t = torch.mul(A.unsqueeze(1), B_pred)  # [ P x K x K ]
                    p_theta_marg = torch.bmm(torch.transpose(q_t, 1,2),  p[i].unsqueeze(2)).squeeze()  # this marginalizes over dim=2
                    p_theta_marg = p_theta_marg / p_theta_marg.sum(axis=1, keepdim=True)
                    #print(p_theta_marg)
                    x_tminus1[i] = torch.multinomial(p_theta_marg, num_samples=1).squeeze()
                    # On final timestep pick next best from non-standard AA
                    if t == 1:
                         x_tminus1[i] = torch.multinomial(p_theta_marg[:, :tokenizer.K-6], num_samples=1).squeeze()
                    # diff = torch.ne(s, x_tminus1[i])
                    # if t % 100 == 0:
                    #     print("time", t, diff.sum().item(), "mutations", tokenizer.untokenize(x_tminus1[i]), "sample", tokenizer.untokenize(s))
                sample = x_tminus1

    untokenized = [tokenizer.untokenize(s) for s in sample]
    print("final seq", untokenized)
    return sample, untokenized

def generate_random_seq(seq_len, train_prob_dist, tokenizer=Tokenizer()):
    """
    Generates a set of random sequences drawn from a train distribution
    """
    all_aas = tokenizer.all_aas
    sample = torch.multinomial(torch.tensor(train_prob_dist), num_samples=seq_len, replacement=True)
    #sample = torch.randint(0, len(all_aas)-4, (seq_len,)) # ignore char (JOU-) in aa dict not accepted by PROSITE
    sample = sample.to(torch.long)
    #print("sequence", tokenizer.untokenize(sample))
    return sample, tokenizer.untokenize(sample)

def generate_valid_subset(data_top_dir='data/', samples=20, by_length=False):
    "Randomly sample from test dataset for comparisons to generated"
    data_valid = UniRefDataset('data/uniref50/', 'rtest', structure=False, max_len=2048)
    # metadata = np.load(data_top_dir + 'uniref50/lengths_and_offsets.npz')
    # ds_valid = UniRefDataset(data_top_dir + 'uniref50/', 'rtest', structure=False)
    # valid_idx = ds_valid.indices
    # len_valid = metadata['ells'][valid_idx]
    # valid_sortish_sampler = SortishSampler(len_valid, 10000, num_replicas=1, rank=0)
    # valid_sampler = ApproxBatchSampler(valid_sortish_sampler, 40000, 256, len_valid)
    # dl_valid = DataLoader(dataset=ds_valid,
    #                       batch_sampler=valid_sampler,
    #                       num_workers=8)
    if by_length:
        #valid_indices = np.sort(np.random.choice(len_valid, 80000, replace=False))
        #sample
        #seq_lengths = [64, 128, 256, 384]
        sample_64 = []
        sample_128 = []
        sample_256 = []
        sample_384 = []
        for i, batch in enumerate(dl_valid):
            for j,seq in enumerate(batch[0]):
                seq_len = len(seq)
                if seq_len >= 40 and seq_len <= 80: # near 62
                    sample_64.append(seq)
                elif seq_len > 100 and seq_len <= 150: # near 128
                    sample_128.append(seq)
                elif seq_len > 230 and seq_len <= 270: # near 256
                    sample_256.append(seq)
                elif seq_len > 490 and seq_len <= 530: # near 384
                    sample_384.append(seq)
                else:
                    pass
        return random.sample(sample_64, samples), random.sample(sample_128, samples), \
                random.sample(sample_256, samples), random.sample(sample_384, samples)
    else:
        sample = []
        for i in tqdm(range(samples)):
            r_idx = np.random.choice(len(data_valid))
            sequence = data_valid[r_idx][0]
            sample.append(sequence)
        print(sample)
        return sample


if __name__ == '__main__':
    main()