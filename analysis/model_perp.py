import numpy as np
from dms.pretrained import CARP_38M, CARP_640M, D3PM_BLOSUM_38M, D3PM_BLOSUM_640M, D3PM_UNIFORM_38M, D3PM_UNIFORM_640M,\
                           OA_AR_640M, OA_AR_38M, LR_AR_38M, LR_AR_640M, ESM1b_640M
from torch.nn import CrossEntropyLoss
from dms.losses import OAMaskedCrossEntropyLoss
import torch
from sequence_models.datasets import UniRefDataset
from tqdm import tqdm
import pandas as pd
from analysis.plot import plot_perp_group_masked, plot_perp_group_d3pm
import math

def main():
    np.random.seed(0) # set random seed

    # Def read seqs from fasta
    data = UniRefDataset('data/uniref50/', 'rtest', structure=False, max_len=1022) # For ESM max_len=1024 (1022+start/stop), for DIFF 2048

    #checkpoint = ESM1b_640M()
    #save_name = 'esm_1b_640M'
    checkpoint = CARP_640M()
    save_name = 'carp_640M'

    perplexities = []
    time_perp_data = []
    for i in tqdm(range(25000)): #len(data))):
        r_idx = np.random.choice(len(data))
        sequence = [data[r_idx]]
        t, p = calculate_perplexity(sequence, checkpoint)
        # This will work most of the time
        perplexities.append(p)
        time_perp_data.append([t,p])
        # Use this only for D3PM
        # if p <= 31: # Ignore weird outliers at high timesteps (400-500) that happen for short sequences
        #     perplexities.append(p)
        #     time_perp_data.append([t,p])
        #ESM generates nans sometimes
        # if math.isnan(p):
        #     pass
        # else:
        #     perplexities.append(p)
        #     time_perp_data.append([t,p])
        # #print(p)
        if i % 1000 == 0:
            print(i, "samples, perp:", np.mean(perplexities))
    print("Final test perp:", np.mean(perplexities))

    df = pd.DataFrame(time_perp_data, columns=['time', 'perplexity'])

    plot_perp_group_masked(df, save_name)
    #plot_perp_group_d3pm(df, save_name)

def calculate_perplexity(sequence, checkpoint):
    model, collater, tokenizer, scheme = checkpoint
    # Use model.eval() if using CPU
    model.eval().cuda()

    # D3PM Collater returns; src, src_one_hot, timesteps, tokenized, tokenized_one_hot, Q, Q_bar, q_x
    if scheme == 'd3pm':
        src, src_onehot, timestep, tgt, tgt_onehot, Q, Q_bar, q = collater(sequence)
    elif scheme == 'mask':
        src, timestep, tgt, mask = collater(sequence)
        input_mask = (src != tokenizer.pad_id).float() # Should be no pads since not batching
        # Comment all variable.cuda() lines if using CPU
        mask = mask.cuda()
        input_mask = input_mask.cuda()
    elif scheme == 'esm-mask':
        src, timestep, tgt, mask = collater(sequence)
        input_mask = (src != tokenizer.padding_idx).float()  # Should be no pads since not batching
        # Comment all variable.cuda() lines if using CPU
        mask = mask.cuda()
        input_mask = input_mask.cuda()
    # Comment all variable.cuda() lines if using CPU
    src = src.cuda()
    timestep = timestep.cuda()
    tgt = tgt.cuda()
    with torch.no_grad():
        outputs = model(src, timestep) # for both d3pm and oaardm this is predicting x_0 (tgt)
        if scheme == 'esm-mask':
            outputs = outputs["logits"]

    # Get loss (NLL ~= CE)
    if scheme == 'd3pm':
        loss_func = CrossEntropyLoss(reduction='sum')
        nll_loss = loss_func(outputs.squeeze(), tgt.squeeze())
        ll = -nll_loss.item() / len(tgt.squeeze()) # over all tokens
        t_out=timestep
    elif scheme == 'mask' or scheme == 'esm-mask':
        #print(outputs, tgt, mask, timestep, input_mask)
        loss_func = OAMaskedCrossEntropyLoss(reweight=False)
        #print(outputs.shape)
        #print(mask.shape)
        ce_loss, nll_loss = loss_func(outputs, tgt, mask, timestep, input_mask)  # returns a sum of losses
        #print(nll_loss)
        ll = -nll_loss.item() / mask.sum().item() # over masked tokens
        #print(ll)
        t_out = int(mask.sum().item())/int(len(tgt.squeeze()))
    # Get perp
    perp = np.exp(-ll)
    #print(perp)
    #print("num mask:", int(mask.sum().item()), "of total:", int(len(tgt.squeeze())), f', Perplexity: {perp:.5f}')
    #print("t:",timestep, f', Perplexity: {perp:.5f}')
    return t_out, perp

if __name__ == '__main__':
    main()