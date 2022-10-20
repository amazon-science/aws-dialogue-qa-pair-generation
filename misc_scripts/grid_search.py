import argparse
import sys
import random
from datetime import datetime

import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau

#BartBase
from transformers import BartTokenizer, BartForConditionalGeneration, AdamW, get_scheduler

#UnifiedQA
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

#Dataloader
from loaders.dataloader_pt import BartBatcher
torch_device = 'cuda' if torch.cuda.is_available() else 'cpu'

def train(args):
    # Model & Optimizer
    if args.base_model == 'bart':
        tokenizer = BartTokenizer.from_pretrained('facebook/bart-large-xsum')
        model = BartForConditionalGeneration.from_pretrained('facebook/bart-large-xsum')
    elif args.base_model == 'unifiedqa':
        tokenizer = AutoTokenizer.from_pretrained("allenai/unifiedqa-t5-large")
        model = AutoModelForSeq2SeqLM.from_pretrained("allenai/unifiedqa-t5-large")
    else:
        print("Base model must be BART or UnifiedQA")
        return
      
    optimizer = optim.Adam(filter(lambda p: p.requires_grad, model.parameters()), lr=args.lr,betas=(0.9,0.999),eps=1e-08,weight_decay=0)
    optimizer.zero_grad()

    # LR Scheduler
    scheduler = ReduceLROnPlateau(optimizer, mode='min', patience=3, verbose=True)

    model_config = model.config
    if torch_device == 'cuda': model.cuda()
    print("#parameters:", sum(p.numel() for p in model.parameters() if p.requires_grad))
    model.train()

    batcher = BartBatcher(tokenizer, model.config, args.train_path, torch_device)

    # Validation 
    val_batcher = BartBatcher(tokenizer, model.config, args.val_path, torch_device)

    # Criterion
    criterion = nn.CrossEntropyLoss(reduction='none') # This criterion combines nn.LogSoftmax() and nn.NLLLoss() in one single class.

    training_step  = 0
    batch_size     = args.bsz
    gradient_accum = args.grad_accum
    valid_step     = 20000 # every a few hours on lapaz machine (1GPU - 1080Ti)
    total_step     = 20000 * 1000
    best_val_loss  = 99999999
    random_seed    = 777
    stop_counter   = 0

    print("batch_size:", batch_size)
    print("training_step:", training_step)
    print("gradient_accum:", gradient_accum)
    print("total_step:", total_step)
    print("valid_step:", valid_step)
    print("random_seed:", random_seed)
    print("pretrain:", batcher.pretrain)
    print("epochs:", args.epochs)
    print("LR: ", lr)

    # Randomness
    random.seed(random_seed)
    torch.manual_seed(random_seed)

    # shuffle data
    batcher.shuffle_data()
    val_batcher.shuffle_data()

    # log file
    log = open(args.log_path, 'w')
    log.write("Learning Rate: {}".format(args.lr))
    log.write('\n')

    if torch.cuda.device_count() > 1:
        print("Multiple GPUs: {}".format(torch.cuda.device_count()))
        model = nn.DataParallel(model)

    current_epoch = 0
    best_epoch = 0
    while batcher.epoch_counter < args.epochs:
        # get a batch
        input_ids, attention_mask, target_ids, target_attention_mask = batcher.get_a_batch(batch_size=batch_size)
        shifted_target_ids, shifted_target_attention_mask = batcher.shifted_target_left(target_ids, target_attention_mask)
    
        # BART forward
        x = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=target_ids,
            decoder_attention_mask=target_attention_mask,
        )

        # x[0] # decoder output
        # x[1] # encoder output
        lm_logits = x[0]

        loss = criterion(lm_logits.view(-1, model_config.vocab_size), shifted_target_ids.view(-1))
        shifted_target_attention_mask = shifted_target_attention_mask.view(-1)
        loss = (loss * shifted_target_attention_mask).sum() / shifted_target_attention_mask.sum()
        loss = loss / gradient_accum # normalize loss for gradient accumulation
        loss.backward()

        if training_step % gradient_accum == 0:
            optimizer.step()
            optimizer.zero_grad()

        #import pdb; pdb.set_trace()
        if (training_step+1) % 1000 == 0:
            print("[{}] step {}/{}: loss = {:.5f}: lr = {}".format(str(datetime.now()), training_step, total_step, loss.item(), str(optimizer.param_groups[0]['lr'])))
            log.write("[{}] step {}/{}: loss = {:.5f}".format(str(datetime.now()), training_step, total_step, loss.item()))
            print("epoch: ", current_epoch)
            log.write('\n')
            sys.stdout.flush()

        #if training_step % valid_step == 0 and training_step > 5:
        if current_epoch+1 == batcher.epoch_counter:
            model.eval()
            with torch.no_grad():
                valid_loss = validation(model, model_config, val_batcher, batch_size)
            print("Valid Loss = {:.5f}".format(valid_loss))
            model.train()
            if valid_loss < best_val_loss:
                stop_counter = 0
                best_val_loss = valid_loss
                best_epoch = current_epoch
                log.write(str(best_val_loss))
                log.write('\n')
                print("Model improved".format(stop_counter))
            else:
                stop_counter += 1
                print("Model not improved #{}".format(stop_counter))
                if stop_counter == args.stop_counter:
                    print("Stop this set.")
                    log.write("Model not improved.")
                    log.close()
                    return best_val_loss, best_epoch
            log.write('\n')
            log.write("LR {}: EPOCH {}: LOSS {}".format(str(optimizer.param_groups[0]['lr']), current_epoch, best_val_loss))
            log.write('\n')
            log.write('\n')
            scheduler.step(valid_loss)
            current_epoch = batcher.epoch_counter
            
        training_step += 1
    log.write('\n')
    log.write("LR {}: EPOCH {}: LOSS {}".format(lr, current_epoch, best_val_loss))
    log.write('\n')
    log.write('\n')
    print("Finish this parameter set.")
    log.close()
    return best_val_loss, best_epoch


def validation(model, model_config, val_batcher, batch_size):
    print("start validating")
    criterion = nn.CrossEntropyLoss(reduction='none')
    sum_loss = 0
    sum_token = 0
    while val_batcher.epoch_counter < 1:
        input_ids, attention_mask, target_ids, target_attention_mask = val_batcher.get_a_batch(batch_size=batch_size)
        shifted_target_ids, shifted_target_attention_mask = val_batcher.shifted_target_left(target_ids, target_attention_mask)
        x = model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=target_ids,
            decoder_attention_mask=target_attention_mask,
        )
        lm_logits = x[0]
        loss = criterion(lm_logits.view(-1, model_config.vocab_size), shifted_target_ids.view(-1))
        shifted_target_attention_mask = shifted_target_attention_mask.view(-1)
        sum_loss += (loss * shifted_target_attention_mask).sum().item()
        sum_token += shifted_target_attention_mask.sum().item()
        #print("#", end="")
        sys.stdout.flush()
    val_batcher.epoch_counter = 0
    val_batcher.cur_id = 0
    print("finish validating")

    return sum_loss / sum_token

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-train_path", default="Data/", type=str, nargs='+')
    parser.add_argument("-val_path", default="Data/", type=str, nargs='+')
    parser.add_argument("-base_model", default="bart", type=str, choices=['bart', 'unifiedqa'], help="Pick which model to use as the base. Choices: bart or unifiedqa")
    parser.add_argument("-log_path", default="logs/log", type=str)
    parser.add_argument("-stop_counter", default=5, type=int)
    parser.add_argument("-bsz", default=1, type=int)
    parser.add_argument("-grad_accum", default=32, type=int)
    parser.add_argument("-epochs", default=10, type=int)
    parser.add_argument("-lr", default = 2e-5, type=float)

    args = parser.parse_args()

    #learning_rates = [.0001, 1e-5, 3e-5, 1e-6, 3e-6, 3e-7]
    learning_rates = [.0001, 1e-5, 3e-5]
    #learning_rates = [1e-5, 3e-5, 1e-6, 3e-6, 3e-7]
    #grad_accum_rates = [16, 32]

    log_path = args.log_path
    
    best_lr = None
    best_val_loss = 99999999
    best_ep = None
    for lr in learning_rates:
        #for ga in grad_accum_rates:
        args.lr = lr
        args.log_path = log_path + "_" + str(lr)
        loss, epoch = train(args)
        print("FINISHED A SET: ", loss)
        if loss < best_val_loss:
            best_lr = lr
            best_val_loss = loss
            best_ep = epoch

    print("best lr: ", best_lr)
    print("best epoch: ", best_ep)
    print("best loss: ", best_val_loss)
