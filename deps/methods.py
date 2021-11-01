from typing import Dict, Tuple, Optional

import numpy
import torchtuples as tt
from optuna import Trial
from pandas import DataFrame
from pycox.models import CoxPH
from sklearn.preprocessing import FunctionTransformer, StandardScaler
from sksurv.ensemble import GradientBoostingSurvivalAnalysis, RandomSurvivalForest

from deps.common import RANDOM_STATE, CACHE_DIR
from hcve_lib.custom_types import Estimator
from hcve_lib.cv import predict_nn, predict_survival
from hcve_lib.data import to_survival_y_records, to_survival_y_pair
from hcve_lib.utils import remove_column_prefix, Debug
from hcve_lib.wrapped_sklearn import DFCoxnetSurvivalAnalysis, DFPipeline, DFColumnTransformer, DFSimpleImputer, \
    DFOrdinalEncoder


def gb_optuna(trial: Trial) -> Tuple[Trial, Dict]:
    hyperparameters = {
        'estimator': {
            'learning_rate': trial.suggest_uniform('estimator_learning_rate', 0, 1),
            'max_depth': trial.suggest_int('estimator_max_depth', 1, 10),
            'n_estimators': trial.suggest_int('estimator_n_estimators', 5, 200),
            'min_samples_split': trial.suggest_int('estimator_min_samples_split', 2, 30),
            'min_samples_leaf': trial.suggest_int('estimator_min_samples_leaf', 1, 200),
            'max_features': trial.suggest_categorical(
                'estimator_max_features', ['auto', 'sqrt', 'log2']
            ),
            'subsample': trial.suggest_uniform('estimator_subsample', 0.1, 1),
        }
    }
    return trial, hyperparameters


def coxnet_optuna(trial: Trial) -> Tuple[Trial, Dict]:
    hyperparameters = {
        'estimator': {
            'l1_ratio': 1 - trial.suggest_loguniform('estimator_n_alphas', 0.1, 1),
        }
    }
    return trial, hyperparameters


def rsf_optuna(trial: Trial) -> Tuple[Trial, Dict]:
    hyperparameters = {
        'estimator': {
            'n_estimators': trial.suggest_int('estimator_n_estimators', 5, 200),
            'max_depth': trial.suggest_int('estimator_max_depth', 1, 30),
            'min_samples_split': trial.suggest_int('estimator_min_samples_split', 2, 30),
            'min_samples_leaf': trial.suggest_int('estimator_min_samples_leaf', 1, 200),
            'max_features': trial.suggest_categorical(
                'estimator_max_features', ['auto', 'sqrt', 'log2']
            ),
            'oob_score': trial.suggest_categorical('estimator_oob_score', [True, False]),
        }
    }
    return trial, hyperparameters


class MethodDefinition:
    ...


METHODS_DEFINITIONS = {
    'coxnet': {
        'get_estimator': lambda X, verbose: get_pipeline(
            DFCoxnetSurvivalAnalysis(fit_baseline_model=True, verbose=verbose),
            X,
        ),
        'process_y': to_survival_y_records,
        'optuna': coxnet_optuna,
        'predict': predict_survival,
    },
    'gb': {
        'get_estimator': lambda X, verbose: get_pipeline(
            GradientBoostingSurvivalAnalysis(random_state=RANDOM_STATE, verbose=verbose),
            X,
        ),
        'process_y': to_survival_y_records,
        'optuna': gb_optuna,
        'predict': predict_survival,
    },
    'rsf': {
        'get_estimator': lambda X, verbose:
        get_pipeline(RandomSurvivalForest(random_state=RANDOM_STATE), X),
        'process_y': to_survival_y_records,
        'optuna': rsf_optuna,
        'predict': predict_survival,
    },
    'pycox': {
        'get_estimator': lambda X, verbose: get_pycox_pipeline(X, verbose=verbose),
        'process_y': to_survival_y_pair,
        'predict': predict_nn,
    }
}


def transform_X(input):
    return numpy.array(input).astype('float32')


from torch import nn


class NNWrapper(CoxPH):
    net: Optional[nn.Module]

    def __init__(self, net=None, *args, **kwargs):
        self.net = net
        self.args = args
        self.kwargs = kwargs

    def fit(self, X, y, **kwargs):
        if not self.net:
            self.net = make_net(X.shape[1])

        super().__init__(self.net, tt.optim.Adam, *self.args, **self.kwargs)
        super().fit(X.astype('float32'), y)
        return self


def make_net(in_features: int) -> nn.Module:
    num_nodes = [32, 32]
    out_features = 1
    batch_norm = True
    dropout = 0.1
    return tt.practical.MLPVanilla(in_features, num_nodes, out_features, batch_norm, dropout)


def get_pycox_pipeline(X: DataFrame, verbose: int = 0):
    preprocessing, categorical_features, continuous_features = get_pipeline_preprocessing(X)

    return DFPipeline(
        [
            *preprocessing,
            ('standard', StandardScaler()),
            ('to_numpy', FunctionTransformer(transform_X)),
            ('estimator', NNWrapper()),
        ],
        memory=CACHE_DIR
    )


def get_pipeline(estimator: Estimator, X: DataFrame) -> DFPipeline:
    preprocessing, _, _ = get_pipeline_preprocessing(X)
    return DFPipeline([
        *preprocessing,
        ('estimator', estimator),
    ],
                      # memory=CACHE_DIR,
                      )


def get_pipeline_preprocessing(X: DataFrame) -> Tuple:
    categorical_features = [
        column_name for column_name in X.columns
        if X[column_name].dtype.name == 'object' or X[column_name].dtype.name == 'category'
    ]

    continuous_features = [
        column_name for column_name in X.columns if column_name not in categorical_features
    ]

    pipeline = [
        (
            'imputer',
            DFColumnTransformer(
                [
                    (
                        'categorical',
                        DFSimpleImputer(strategy='most_frequent'),
                        categorical_features,
                    ),
                    (
                        'continuous',
                        DFSimpleImputer(strategy='mean'),
                        continuous_features,
                    ),
                ],
            )
        ),
        (
            'remove_prefix',
            FunctionTransformer(remove_column_prefix),
        ),
        # TODO
        # ('imputer', MiceForest(random_state=5465132, iterations=5)),
        (
            'scaler',
            DFColumnTransformer(
                [
                    (
                        'categorical',
                        DFOrdinalEncoder(handle_unknown='use_encoded_value', unknown_value=-1),
                        categorical_features,
                    )
                ],
                remainder='passthrough',
            )
        ),
        (
            'remove_prefix2',
            FunctionTransformer(remove_column_prefix),
        ),
    ]

    return pipeline, categorical_features, continuous_features
