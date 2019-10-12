import pandas as pd
from pandas.api.types import CategoricalDtype
import numpy as np
from .utils import VarType
import collections

DEFAULT_NA_VALUES = (98.0, 99.0)

class DataLoader(object):
    def __init__(self, variables, case_id_fun):
        self.variables = variables
        self.case_id_fun = case_id_fun
        self._warnings = []
        
    def add_warning(self, case_id, text):
        '''Logs a warning.
        
        In this context, a warning has an identifier and a text. The identifier should help
        locate which data triggered the warning.
        Warnings will be included as a TableResult with ID root.warnings.
        
        Args:
            case_id (str or pandas.Series): if str, should identify the data that triggered the warning.
                If pd.Series, an string identifier will be generated by
                invoking the case_id_fun provided to the constructor.

            text (str): Warning text.
        '''
        if isinstance(case_id, pd.Series):
            case_id = self.case_id_fun(case_id)
        self._warnings.append((case_id, text))
        
    def load_data(self, fname, ctx, na_values=[' ']):
        '''Loads data from the CSV file identified by fname and pre-processes it.

        The variable names to load are taken from the variable metadata passed to the constructor.
        Only variables without a 'computation' key in their dict will be loaded.
        na_values will be forwarded to pandas.read_csv.
        After loading, data will be pre-processed:
        
            - Derived variables (those defining a 'computation' key in their dict) will be computed.
            - A copy of the original values will be stored in the dataframe, under the name
              $NAME_ORIGINAL, being $NAME the original variable name.
            - Type transformations will be applied.
            - Mandatory checks will be performed. Any row with a NaN value in any mandatory
              variable will be dropped.

        Args:
            fname (str): path to the CSV file to load.
            ctx (ResultTree): warnings will add a table result to the result tree.
            na_values (list of str): values to be considered as NaN.
        Returns:
            Dataframe with the loaded data.
        '''
        csv_varnames = [varname for varname, var in self.variables.items()
                        if all(k not in var for k in ('computation-pre', 'computation-post'))]
        df = pd.read_csv(fname, usecols=csv_varnames, na_values=na_values)
        self._preprocess(df)
        ctx.get_result('root').add_table('warnings', 'Warnings',
                                         headings=['Caso', ''], rows=self._warnings)
            
        return df
        
    def _drop_na(self, df, varname):
        lost_cases = df[df[varname].isna()]
        if len(lost_cases) != 0:
            for _, row in lost_cases.iterrows():
                self.add_warning(row, '{} perdida'.format(varname))
            df.dropna(subset=[varname], inplace=True)
            
    def _compute_derived(self, df, calculation_key):
        for varname, var in self.variables.items():
            fun = var.get(calculation_key)
            if fun is not None:
                df[varname] = fun(df, self)
                
    def _preprocess(self, df):
        self._compute_derived(df, 'computation-pre')
        for varname in df:
            vardata = self.variables[varname]
            vartype = vardata['type']
            
            # Store the original value
            df[varname + '_ORIGINAL'] = df[varname]
    
            # Convert to adequate type
            if vartype == VarType.Category:
                labels = vardata['labels']
                ordered = isinstance(labels, collections.OrderedDict)
                cat_type = CategoricalDtype(categories=labels.keys(), ordered=ordered)
                cat_map = { key: value[0] for key, value in vardata['labels'].items() }
                df.loc[:, varname] = df[varname].astype(cat_type)
                df[varname].cat.rename_categories(cat_map, inplace=True)
            elif vartype == VarType.Bool:
                df.loc[~df[varname].isin((0, 1)), [varname]] = np.nan
                
            # Mandatory checking
            if vardata.get('mandatory', False):
                self._drop_na(df, varname)
                # Further conversion can be done if no NaN is present
                if vartype == VarType.Int:
                    df[varname] = df[varname].astype(np.int64)
        self._compute_derived(df, 'computation-post')
                    
def _combine_variables_row(row, varname1, varname2, on_conflict, na_values, loader):
    v1 = row[varname1]
    v2 = row[varname2]
    is_nan_1 = (np.isnan(v1) or v1 in na_values)
    is_nan_2 = (np.isnan(v2) or v2 in na_values)
    if is_nan_1 and is_nan_2:
        return np.nan
    elif not is_nan_1 and is_nan_2:
        return v1
    elif is_nan_1 and not is_nan_2:
        return v2
    elif v1 == v2:
        return v1
    else: # Conflict
        res = on_conflict(v1, v2) if callable(on_conflict) else on_conflict
        if np.isnan(res):
            loader.add_warning(row, 'Valores contradictorios: {}={} vs. {}={}'.format(varname1, v1, varname2, v2))
        return res
    
def _multibool_to_enum_row(row, variables, na_values, loader):
    row_fragment = row[variables]
    num_vars = len(variables)
    if row_fragment.isna().sum() + row_fragment.isin(na_values).sum() == num_vars:
        return np.nan
    true_vars = row_fragment[row_fragment == 1.0]
    if len(true_vars) != 1:
        value_list = ', '.join('{}={}'.format(varname, row_fragment[varname]) for varname in row_fragment.index)
        loader.add_warning(row, 'Multi-bool a enum - valores contradictorios: {}'.format(value_list))
        return np.nan
    return true_vars.index[0]

def combine_variables(varname1, varname2, on_conflict=np.nan, na_values=DEFAULT_NA_VALUES):
    def res(df, loader):
        return df.apply(lambda row: _combine_variables_row(
            row, varname1, varname2, on_conflict, na_values, loader), axis=1)
    return res

def combine_variables_bool(varname1, varname2, na_values=DEFAULT_NA_VALUES):
    return combine_variables(varname1, varname2, lambda v1, v2: v1 or v2, na_values)

def _logical_op(true_combinator, false_combinator, *varnames):
    assert(len(varnames) >= 2)
    def res(df, loader):
        true_values = df[varnames[0]] == 1.0
        false_values = df[varnames[0]] == 0.0
        for vname in varnames:
            true_values = true_combinator(true_values, df[vname] == 1.0)
            false_values = false_combinator(false_values, df[vname] == 0.0)
        result_series = pd.Series(np.nan, index=df.index)
        result_series[true_values] = 1.0
        result_series[false_values] = 0.0
        return result_series
    return res

def logical_or(*varnames):
    return _logical_op(lambda x, y: x | y, lambda x, y: x & y, *varnames)

def logical_and(*varnames):
    return _logical_op(lambda x, y: x & y, lambda x, y: x | y, *varnames)

def multibool_to_enum(variables, na_values=DEFAULT_NA_VALUES):
    def res(df, loader):
        return df.apply(lambda row: _multibool_to_enum_row(
            row, variables, na_values, loader), axis=1)
    return res
