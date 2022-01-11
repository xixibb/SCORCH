#######################################################################
# MLScoring script version 1.0 - Run python scoring.py -h for help    #
#                                                                     #
# Script Authors:                                                     #
# @sammoneykyrle                                                      #
# @milesmcgibbon                                                      #
#                                                                     #
# School of Biological Sciences                                       #
# The University of Edinburgh                                         #
#######################################################################


# import all libraries and ignore tensorflow warnings
import textwrap
import time
import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
import tensorflow as tf
tf.get_logger().setLevel('ERROR')
from tensorflow.keras.models import load_model
from utils import binana
from sys import platform
from utils import kier
import logging
from utils.ecifs import *
from utils.dock_functions import *
import pandas as pd
import multiprocessing as mp
import sys
import joblib
from functools import reduce
import pickle
import numpy as np
import shutil
import json
import xgboost as xgb
from tqdm import tqdm
from warnings import filterwarnings
from itertools import product, chain
from functools import partialmethod
filterwarnings('ignore')

# get working directory where scoring function is being deployed
stem_path = os.getcwd()

def run_binana(params, lig, rec):

    ###########################################
    # Function: Get BINANA descriptors for    #
    # protein-ligand complex                  #
    #                                         #
    # Inputs: BINANA parameters dictionary,   #
    # ligand as a pdbqt string block,         #
    # receptor pdbqt filepath                 #
    #                                         #
    # Output: BINANA protein-ligand complex   #
    # descriptor features as a DataFrame      #
    ###########################################

    if params.okay_to_proceed() == False:
        print(
            "Error: You need to specify the ligand and receptor PDBQT files to analyze using\nthe -receptor and -ligand tags from the command line.\n"
        )
        sys.exit(0)

    if params.error != "":
        print("Warning: The following command-line parameters were not recognized:")
        print(("   " + cmd_params.error + "\n"))

    output = binana.Binana(lig, rec, params).out

    return binana.parse(output, 0)

def kier_flexibility(lig):

    ###########################################
    # Function: Calculate Kier flexibility    #
    # for ligand                              #
    #                                         #
    # Inputs: ligand as a pdbqt string block  #
    #                                         #
    # Output: Kier flexibility                #
    ###########################################

    mol = kier.SmilePrep(lig)
    return kier.CalculateFlexibility(mol)

def calculate_ecifs(lig, rec):

    ###########################################
    # Function: Get ECIFs for protein-ligand  #
    # complex                                 #
    #                                         #
    # Inputs: ligand as a pdbqt string block, #
    # receptor pdbqt filepath                 #
    #                                         #
    # Output: ECIF protein-ligand complex     #
    # descriptor features as a DataFrame      #
    ###########################################

    ECIF_data = GetECIF(rec, lig, distance_cutoff=6.0)
    ECIFHeaders = [header.replace(';','') for header in PossibleECIF]
    ECIF_data = dict(zip(ECIFHeaders,ECIF_data))
    ECIF_df = pd.DataFrame(ECIF_data,index=[0])

    return ECIF_df

def extract(params):

    ###########################################
    # Function: Get all descriptor features   #
    # for protein-ligand complex              #
    #                                         #
    # Inputs: User defined params dictionary  #
    #                                         #
    # Output: All protein-ligand complex      #
    # descriptor features as a DataFrame      #
    ###########################################

    lig = params.params["ligand"]
    rec = params.params["receptor"]
    k = kier_flexibility(lig)
    bin = run_binana(params,lig,rec)
    ECIF = calculate_ecifs(lig, rec)
    df = pd.concat([ECIF,bin],axis=1)
    df['Kier Flexibility'] = k
    return df

def transform_df(df, single):

    ###########################################
    # Function: Condense and scale descriptor #
    # features for model input                #
    #                                         #
    # Inputs: Full Dataframe of               #
    # protein-ligand complex descriptors,     #
    # boolean for single pose model type,     #
    # boolean for further condensation with   #
    # principle component analysis            #
    #                                         #
    # Output: DataFrame of features for model #
    # input                                   #
    ###########################################

    reference_headers = json.load(open(os.path.join('utils','params','features.json')))
    scaler_14 = reference_headers.get('for_scaler_14')
    headers_14 = reference_headers.get('490_models_14')
    scaler_58 = reference_headers.get('for_scaler_58')
    headers_58 = reference_headers.get('492_models_58')
    if single:
        df = df[scaler_14]
        scaler = joblib.load(os.path.join('utils','params','14_maxabs_scaler_params.save'))
        scaled = scaler.transform(df)
        df[df.columns] = scaled
        df = df[headers_14]

    else:
        df = df[scaler_58]
        scaler = joblib.load(os.path.join('utils','params','58_maxabs_scaler_params.save'))
        scaled = scaler.transform(df)
        df[df.columns] = scaled
        df = df[headers_58]

    return df

def multiple_pose_check(lig, pose_1):

    ###########################################
    # Function: Transform ligand.pdbqt        #
    # poses/models into pdbqt string blocks   #
    #                                         #
    # Inputs: ligand.pdbqt filepath           #
    #                                         #
    # Output: List of model/pose pdbqt string #
    # blocks                                  #
    ###########################################

    pdbqt_pose_blocks = list()
    lig_text = open(lig, 'r').read()
    lig_poses = lig_text.split('MODEL')
    for pose in lig_poses:
        lines = pose.split('\n')
        clean_lines = [line for line in lines if not line.strip().lstrip().isnumeric() and 'ENDMDL' not in line]
        if len(clean_lines) < 3:
            pass
        else:
            pose = '\n'.join(clean_lines)
            pdbqt_pose_blocks.append(pose)

    pdbqt_pose_blocks = list(map(lambda x: (f'_pose_{pdbqt_pose_blocks.index(x) + 1}', x), pdbqt_pose_blocks))

    return pdbqt_pose_blocks

def run_networks(df, model_file, model_name):

    models_to_load = model_file

    ###########################################
    # Function: Get prediction from MLP model #
    # for protein-ligand complex              #
    #                                         #
    # Inputs: Number of networks as integer,  #
    # condensed protein-ligand complex        #
    # features as DataFrame                   #
    #                                         #
    # Output: Float prediction                #
    ###########################################


    predictions = pd.DataFrame()
    model_columns = list()

    for i in tqdm(range(len(models_to_load))):
        model = load_model(models_to_load[i])
        y_pred = model.predict(df)
        model_columns.append(f'{model_name}_{i + 1}')
        predictions[f'{model_name}_{i + 1}'] = y_pred.flatten()

    predictions[f'{model_name}_best_average'] = predictions[model_columns].mean(axis=1)

    return predictions.reset_index(drop=True)

def run_xgbscore(df, single):

    ###########################################
    # Function: Get prediction from XGB model #
    # for protein-ligand complex              #
    #                                         #
    # Inputs: Condensed protein-ligand        #
    # complex features as DataFrame,          #
    # boolean for single pose model           #
    #                                         #
    # Output: Float prediction                #
    ###########################################

    global xgbscore
    dtest = xgb.DMatrix(df, feature_names=df.columns)
    prediction = xgbscore.predict(dtest)
    return prediction

def test(params):

    ###########################################
    # Function: Wrapper to score              #
    # single protein-ligand complex/pose      #
    #                                         #
    # Inputs: User defined params dictionary  #
    #                                         #
    # Output: Float single prediction or      #
    # consensus mean prediction from all      #
    # three models                            #
    ###########################################

    cmd_params = binana.CommandLineParameters(params['binana_params'].copy())
    features = extract(cmd_params)
    results = []

    if params['mlpscore_multi'] == True:
        df = transform_df(features, single=True)
        mlp_result = run_networks(params['num_networks'],df,'mlpscore_multi')
        results.append(mlp_result)

    if params['wdscore_multi'] == True:
        df = transform_df(features, single=True)
        mlp_result = run_networks(params['num_networks'],df,'wdscore_multi')
        results.append(mlp_result)

    if params['xgbscore_multi'] == True:
        df = transform_df(features, single=False)
        xgb_result = run_xgbscore(df, single=False)
        results.append(xgb_result)

    return np.mean(results)

def binary_concat(dfs, headers):

    ###########################################
    # Function: Concatenate list of           #
    # dataframes into a single dataframe by   #
    # sequentially writing to a single binary #
    # file (removes pd.concat bottleneck)     #
    #                                         #
    # Inputs: List of dataframes, dataframe   #
    # headers as a list                       #
    #                                         #
    # Output: Single combined dataframe       #
    ###########################################

    total_rows = 0
    with open(os.path.join('utils','temp','features.bin'),'wb') as binary_store:
        for df in dfs:
            df['nRot'] = pd.to_numeric(df['nRot'])
            rows, fixed_total_columns = df.shape
            total_rows += rows
            binary_store.write(df.values.tobytes())
            typ = df.values.dtype

    with open(os.path.join('utils','temp','features.bin'),'rb') as binary_store:
        buffer = binary_store.read()
        data = np.frombuffer(buffer, dtype=typ).reshape(total_rows, fixed_total_columns)
        master_df = pd.DataFrame(data = data, columns = headers)
    os.remove(os.path.join('utils','temp','features.bin'))
    return master_df

def parse_args(args):

    ###########################################
    # Function: Parse user defined command    #
    # line arguments                          #
    #                                         #
    # Inputs: Command line arguments          #
    #                                         #
    # Output: Populated params dictionary     #
    ###########################################

    params = {}

    if '-h' in args:
        prefix = "\t\t"
        expanded_indent = textwrap.fill(prefix+'$', replace_whitespace=False)[:-1]
        subsequent_indent = ' ' * len(expanded_indent)
        wrapper = textwrap.TextWrapper(initial_indent=prefix,
                                       subsequent_indent=subsequent_indent)
        with open(os.path.join('utils','help_string.txt')) as help_string:
            help = help_string.read()
            for line in help.split('\n'):
                if line.isupper():
                    print(line)
                elif  '-' in line:
                    print(line)
                else:
                    print(wrapper.fill(line))
        sys.exit()

    try:
        params['binana_params'] = ['-receptor', args[args.index('-receptor') + 1], '-ligand', args[args.index('-ligand') + 1]]
        params['ligand'] = args[args.index('-ligand') + 1]
        params['receptor'] = args[args.index('-receptor') + 1]
        try:
            params['threads'] = int(args[args.index('-threads') + 1])
        except:
            params['threads'] = 1
        try:
            params['ref_lig'] = args[args.index('-ref_lig') + 1]
        except:
            params['ref_lig'] = None
        try:
            params['out'] = args[args.index('-out') + 1]
        except:
            params['out'] = False

        models = ['-mlpscore_multi','-wdscore_multi','-xgbscore_multi']
        args_check = list(map(lambda v: v in args, models))
        if any(args_check):
            for model, check in zip(models,args_check):
                params[model] = args_check
        else:
            for model in models:
                params[model.replace('-','')] = True

        params['dir'] = False
        params['screen'] = False
        params['single'] = False
        params['pose_1'] = False
        params['dock'] = False
        params['concise'] = True
        params['num_networks'] = 15


        if '-verbose' in args:
            params['verbose'] = True
            logging.basicConfig(level=logging.INFO, format='%(message)s')
        else:
            params['verbose'] = False
            tqdm.__init__ = partialmethod(tqdm.__init__, disable=True)
            logging.basicConfig(level=logging.CRITICAL, format='%(message)s')

        if '-pose_1' in args:
            params['pose_1'] = True

        if '-detailed' in args:
            params['concise'] = False

        if params['ligand'] == params['receptor']:
            receptors = list()
            ligands = list()
            params['dir'] = True
            folders = os.listdir(params['ligand'])
            for folder in folders:
                files_in_folder = os.listdir(f'{params["ligand"]}{folder}')
                receptor = [os.path.join(f'{params["ligand"]}{folder}',f'{f}') for f in files_in_folder if 'receptor' in f][0]
                receptors.append(receptor)
                ligand = [os.path.join(f'{params["ligand"]}{folder}',f'{f}') for f in files_in_folder if 'ligand' in f][0]
                ligands.append(ligand)
            params['ligand'] = ligands
            params['receptor'] = receptors

        elif os.path.isdir(params['ligand']) == True:
            params['ligand'] = [os.path.join(params['ligand'], file) for file in os.listdir(params['ligand'])]
            receptors = [params['receptor'] for i in range(len(params['ligand']))]
            params['receptor'] = receptors
            params['screen'] = True

        elif '.smi' in params['ligand'] or '.txt' in params['ligand']:
            params['dock'] = True


        else:
            params['ligand'] = [params['ligand']]
            params['receptor'] = [params['receptor']]
            params['single'] = True

        if '-num_networks' in args:
            params['num_networks'] = int(args[args.index('-num_networks') + 1])


    except ValueError as e:
        if 'is not in list' in str(e):
            missing = str(e).replace("'",'').replace(' is not in list','')
            print(f'Error: essential argument {missing} not supplied')
            print('Run "python scoring.py -h" for usage instructions')
            sys.exit()



    return params

def prepare_features(receptor_ligand_args):

    ###########################################
    # Function: Wrapper to prepare            #
    # all requested protein-ligand            #
    # complexes/poses for scoring             #
    #                                         #
    # Inputs: User defined params dictionary  #
    # (as global),                            #
    # dictionary of paired ligand/receptor    #
    # filepaths                               #
    #                                         #
    # Output: Writes results as row to output #
    # csv file                                #
    ###########################################

    params = receptor_ligand_args[1]
    receptor_ligand_args = receptor_ligand_args[0]

    receptor = receptor_ligand_args[0]
    ligand = receptor_ligand_args[1]

    lig_block = receptor_ligand_args[2]
    head, name = os.path.split(ligand)
    pose_number = lig_block[0]
    lig_block = lig_block[1]

    ligand_name = name.replace('.pdbqt', pose_number)
    receptor_name = os.path.split(receptor)[-1]
    params['binana_params'][1] = receptor
    params['binana_params'][3] = lig_block
    cmd_params = binana.CommandLineParameters(params['binana_params'].copy())
    features = extract(cmd_params)

    return (receptor_name, ligand_name, features)

def score(models):

    ###########################################
    # Function: Score supplied ligands with   #
    # an individual model                     #
    #                                         #
    # Inputs: Tuple of (model_name,           #
    #                   model_binary_file,    #
    #                   feature dataframes)   #
    #                                         #
    # Output: Dataframe of model predictions  #
    ###########################################

    model_name = models[0]

    model_file = models[1]

    features = models[2]

    logging.info(f'Scoring with {model_name}...')

    results = features[['Ligand','Receptor']].copy().reset_index(drop=True)
    df = features.drop(['Ligand','Receptor'], axis=1)

    if 'xgb' in model_name:
        dtest = xgb.DMatrix(df, feature_names=df.columns)
        results[model_name] = model_file.predict(dtest)

    else:
        network_predictions = run_networks(df, model_file, model_name)
        results[network_predictions.columns] = network_predictions[network_predictions.columns]

    return results

def multiprocess_wrapper(function, items, threads):

    ###########################################
    # Function: Parallelise scoring           #
    # protein/ligand complexes                #
    #                                         #
    # Inputs: Function to parallelise         #
    # (def score),                            #
    # list of tuples as function input,       #
    # number of threads to parallelise across #
    #                                         #
    # Output: List of returned results        #
    ###########################################

    processes = min(threads, mp.cpu_count())
    with mp.Pool(processes) as p:
        results = list(tqdm(p.imap(function, items), total=len(items)))
        p.close()
        p.join()

    return results

def print_intro(params):

    ###########################################
    # Function: Prints chosen arguments to    #
    # stdout                                  #
    #                                         #
    # Inputs: User command line parameters    #
    # dictionary                              #
    #                                         #
    # Output: None                            #
    ###########################################


    logging.info('\n')
    logging.info('**************************************************************************')

    logging.info('MLScoring v1.0')
    logging.info('Miles McGibbon, Samuel Money-Kyrle, Vincent Blay & Douglas R. Houston\n')

    logging.info('**************************************************************************\n')

    if params['dir']:
        logging.info(f'Parsed {len(params["ligand"])} protein-ligand complexes for scoring...\n')

    if params['screen']:
        logging.info(f'Parsed {len(params["ligand"])} ligands for scoring against a single receptor...\n')

    if params['single']:
        logging.info('Parsed one ligand for scoring against a single receptor...\n')

    if params['dock']:
        ligand_count = len(open(params["ligand"]).read().split("\n"))
        logging.info(f'Parsed {ligand_count} ligand smiles for docking and scoring against a single receptor...\n')

        logging.info('**************************************************************************\n')

def prepare_models(params):

    ###########################################
    # Function: Loads machine-learning model  #
    # binaries                                #
    #                                         #
    # Inputs: User command line parameters    #
    # dictionary                              #
    #                                         #
    # Output: Dictionary of {model_name:      #
    #                        model_binary}    #
    ###########################################


    logging.info('**************************************************************************\n')
    logging.info('Model Request Summary:\n')

    models = {}

    if params['xgbscore_multi']:

        logging.info('XGBoost Multi-pose Model: Yes')
        xgb_path = os.path.join('utils','models','xgbscore','495_models_58_booster.pkl')
        models['xgbscore_multi'] = pickle.load(open(xgb_path,'rb'))
    else:

        logging.info('XGBoost Multi-pose Model: No')

    if params['mlpscore_multi']:

        logging.info('ANN Multi-pose Model : Yes')
        logging.info(f'- Using Best {params["num_networks"]} Networks')
        models['mlpscore_multi'] = os.path.join('utils','models','mlpscore_multi')
        model_ranks = pickle.load(open(os.path.join(models['mlpscore_multi'],'rankings.pkl'),'rb'))
        model_ranks = model_ranks[:params["num_networks"]]
        models['mlpscore_multi'] = [os.path.join(models['mlpscore_multi'], 'models',f'{model[1]}.hdf5') for model in model_ranks]
    else:

        logging.info('ANN Multi-pose Model: No')

    if params['wdscore_multi']:

        logging.info('WD Multi-pose Model : Yes')
        logging.info(f'- Using Best {params["num_networks"]} Networks')
        models['wdscore_multi'] = os.path.join('utils','models','wdscore_multi')
        model_ranks = pickle.load(open(os.path.join(models['wdscore_multi'],'rankings.pkl'),'rb'))
        model_ranks = model_ranks[:params["num_networks"]]
        models['wdscore_multi'] = [os.path.join(models['wdscore_multi'], 'models',f'{model[1]}.hdf5') for model in model_ranks]
    else:

        logging.info('WD Multi-pose Model: No')

    logging.info('\n')

    if params['pose_1']:

        logging.info('Calculating scores for first model only in pdbqt file(s)\n')


    logging.info('**************************************************************************\n')

    return models

def scoring(params):

    ###########################################
    # Function: Score protein-ligand          #
    # complex(es)                             #
    #                                         #
    # Inputs: User command line parameters    #
    # dictionary                              #
    #                                         #
    # Output: Dataframe of scoring function   #
    # predictions                             #
    ###########################################

    print_intro(params)

    if params['dock']:
        if params['ref_lig'] is None:
            print('ERROR - No reference ligand supplied')
            sys.exit()
        else:
            dock_settings = json.load(open(os.path.join('utils','params','dock_settings.json')))

            pdbs = get_filepaths(os.path.join('utils','temp','pdb_files',''))
            for pdb in pdbs:
                os.remove(pdb)

            pdbqts = get_filepaths(os.path.join('utils','temp','pdbqt_files',''))
            for pdbqt in pdbqts:
                os.remove(pdbqt)

            docked_pdbqts = get_filepaths(os.path.join('utils','temp','docked_pdbqt_files',''))
            for docked_pdbqt in docked_pdbqts:
                os.remove(docked_pdbqt)

            coords = get_coordinates(params['ref_lig'], dock_settings['padding'])
            smi_dict = get_smiles(params['ligand'])

            multiprocess_wrapper(make_pdbs_from_smiles, smi_dict.items(), params['threads'])

            pdbs = os.listdir(os.path.join('utils','temp','pdb_files',''))
            logging.info('Converting pdbs to pdbqts...')
            merged_pdb_args = merge_args(os.path.join('utils','MGLTools-1.5.6',''), pdbs)

            multiprocess_wrapper(autodock_convert, merged_pdb_args.items(), params['threads'])

            pdbqts = get_filepaths(os.path.join('utils','temp','pdbqt_files',''))

            if platform.lower() == 'darwin':
                os_name = 'mac'
            elif 'linux' in platform.lower():
                os_name = 'linux'

            for pdbqt in tqdm(pdbqts):
                dock_file(
                          os.path.join('utils','gwovina-1.0','build',os_name,'release','gwovina'),
                          params['receptor'],
                          pdbqt,
                          *coords,
                          dock_settings['gwovina_settings']['exhaustiveness'],
                          dock_settings['gwovina_settings']['num_wolves'],
                          dock_settings['gwovina_settings']['num_modes'],
                          dock_settings['gwovina_settings']['energy_range'],
                          outfile=os.path.join(f'{stem_path}','utils','temp','docked_pdbqt_files',f'{os.path.split(pdbqt)[1]}')
                          )


            if '.' in params['ligand']:
                docked_ligands_folder = os.path.basename(params['ligand']).split('.')[0]
            else:
                docked_ligands_folder = os.path.basename(params['ligand'])

            docked_ligands_path = os.path.join('docked_ligands',docked_ligands_folder,'')


            params['ligand'] = [os.path.join('utils','temp','docked_pdbqt_files', file) for file in os.listdir(os.path.join('utils','temp','docked_pdbqt_files'))]
            receptors = [params['receptor'] for i in range(len(params['ligand']))]
            params['receptor'] = receptors

            if not os.path.isdir('docked_ligands'):
                os.mkdir('docked_ligands')
            if not os.path.isdir(docked_ligands_path):
                os.makedirs(docked_ligands_path)

            for file in params['ligand']:
                shutil.copy(file, docked_ligands_path)


    poses = list(map(lambda x: multiple_pose_check(x, params['pose_1']), params['ligand']))

    if params['pose_1']:
        poses = [pose[0] for pose in poses]
        receptor_ligand_args = list(zip(params['receptor'], params['ligand'], poses))
    else:
        receptor_ligand_args = list(map(lambda x,y,z: product([x],[y],z),params['receptor'],params['ligand'],poses))
        receptor_ligand_args = list(chain.from_iterable(receptor_ligand_args))

    receptor_ligand_args  = list(map(lambda x: (x, params), receptor_ligand_args))
    features = multiprocess_wrapper(prepare_features, receptor_ligand_args, params['threads'])
    feature_headers = list(features[0][2])
    features_df = binary_concat([i[2] for i in features], feature_headers)
    multi_pose_features = transform_df(features_df, single=False)
    multi_pose_features['Receptor'] = [i[0] for i in features]
    multi_pose_features['Ligand'] = [i[1] for i in features]

    models = prepare_models(params)
    models = list(models.items())
    models = [(m[0], m[1], multi_pose_features) for m in models]

    model_results = list()

    for model in models:
        model_results.append(score(model))
        logging.info('Done!')


    logging.info('**************************************************************************\n')

    merged_results = reduce(lambda x, y: pd.merge(x, y, on = ['Receptor','Ligand']), model_results)

    multi_models = ['xgbscore_multi',
                    'mlpscore_multi_best_average',
                    'wdscore_multi_best_average']

    merged_results['multi_consensus'] = merged_results[multi_models].mean(axis=1)
    merged_results['multi_consensus_stdev'] = merged_results[multi_models].std(axis=1, ddof=0)
    merged_results['multi_consensus_range'] = merged_results[multi_models].max(axis=1) - merged_results[multi_models].min(axis=1)

    if params['concise']:
        merged_results = merged_results[['Receptor',
                                         'Ligand',
                                         'xgbscore_multi',
                                         'mlpscore_multi_best_average',
                                         'wdscore_multi_best_average',
                                         'multi_consensus',
                                         'multi_consensus_stdev',
                                         'multi_consensus_range']].copy()

    return merged_results

if __name__ == "__main__":

    params = parse_args(sys.argv)
    scoring_function_results = scoring(params)
    if not params['out']:
        sys.stdout.write(scoring_function_results.to_csv(index=False))
    else:
        scoring_function_results.to_csv(params['out'], index=False)
