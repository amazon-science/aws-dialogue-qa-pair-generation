import sklearn
from sklearn.metrics import cohen_kappa_score

import statsmodels
from statsmodels.stats.inter_rater import fleiss_kappa
from statsmodels.stats import inter_rater as irr

import json
import numpy as np
from os import listdir
from os.path import isfile, join

def iaa_fleiss(f): # f is file for one example
    data = convert_to_array(f)
    # get score
    ref_data = reformat_data(data)
    print(ref_data)
    return fleiss_kappa(ref_data)

def iaa_cohen(f):
    scores = []
    data = convert_to_array(f)
    for i in range(0, len(data)):
        for j in range(i+1, len(data)):
            scores.append( cohen_kappa_score(data[i], data[j]) )
    return scores

def convert_to_array(f):
    data = []
    # open file
    with open(f, 'r') as f_in:
        x = json.load(f_in)
        for worker in x["answers"]:
            row = []
            answers = worker["answerContent"] # dictionary with selection line by line
            num_lines = len(answers.keys()) # number of lines
            # put the line by line scores into an array
            for i in range(0,num_lines):
                key = "line{}".format(str(i))
                response = answers[key]["selected"]
                if response:
                    row.append(1)
                else:
                    row.append(0)
            data.append(row)
    return data

def reformat_data(data): # data is each row is an annotator for a dialogue
    n_lines = len(data[0])
    n_annotators = len(data)
    t = np.transpose(data)

    new = []
    for line in t:
        trues = sum(line)
        falses = n_annotators - trues
        new.append([falses, trues])
    return new



mypath = "responses_round1/"
files = []
for d in listdir(mypath):
    files = files + [join(mypath+d, f) for f in listdir(mypath+d) if isfile(join(mypath+d, f))]


mypath = "my_responses/"
files_mine = []
for d in listdir(mypath):
    files_mine = files_mine + [join(mypath+d, f) for f in listdir(mypath+d) if isfile(join(mypath+d, f))]

files.sort()
files_mine.sort()

for i in range(0, 5):
    annotators = convert_to_array(files[i])
    me = convert_to_array(files_mine[i])[0]
    for a in annotators:
        print(a)
        print(me)
        score = cohen_kappa_score(a, me)
        print(i, score)
