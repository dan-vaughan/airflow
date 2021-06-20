# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import functools
import inspect
import re
from inspect import signature
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar, cast

from airflow.exceptions import AirflowException
from airflow.models import BaseOperator
from airflow.models.dag import DAG, DagContext
from airflow.models.xcom_arg import XComArg
from airflow.utils.task_group import TaskGroup, TaskGroupContext


def validate_python_callable(python_callable):
    """
    Validate that python callable can be wrapped by operator.
    Raises exception if invalid.

    :param python_callable: Python object to be validated
    :raises: TypeError, AirflowException
    """
    if not callable(python_callable):
        raise TypeError('`python_callable` param must be callable')
    if 'self' in signature(python_callable).parameters.keys():
        raise AirflowException('@task does not support methods')


def get_unique_task_id(
    task_id: str, dag: Optional[DAG] = None, task_group: Optional[TaskGroup] = None
) -> str:
    """
    Generate unique task id given a DAG (or if run in a DAG context)
    Ids are generated by appending a unique number to the end of
    the original task id.

    Example:
      task_id
      task_id__1
      task_id__2
      ...
      task_id__20
    """
    dag = dag or DagContext.get_current_dag()
    if not dag:
        return task_id

    # We need to check if we are in the context of TaskGroup as the task_id may
    # already be altered
    task_group = task_group or TaskGroupContext.get_current_task_group(dag)
    tg_task_id = task_group.child_id(task_id) if task_group else task_id

    if tg_task_id not in dag.task_ids:
        return task_id
    core = re.split(r'__\d+$', task_id)[0]
    suffixes = sorted(
        [
            int(re.split(r'^.+__', task_id)[1])
            for task_id in dag.task_ids
            if re.match(rf'^{core}__\d+$', task_id)
        ]
    )
    if not suffixes:
        return f'{core}__1'
    return f'{core}__{suffixes[-1] + 1}'


class DecoratedOperator(BaseOperator):
    """
    Wraps a Python callable and captures args/kwargs when called for execution.

    :param python_callable: A reference to an object that is callable
    :type python_callable: python callable
    :param op_kwargs: a dictionary of keyword arguments that will get unpacked
        in your function (templated)
    :type op_kwargs: dict
    :param op_args: a list of positional arguments that will get unpacked when
        calling your callable (templated)
    :type op_args: list
    :param multiple_outputs: if set, function return value will be
        unrolled to multiple XCom values. Dict will unroll to xcom values with keys as keys.
        Defaults to False.
    :type multiple_outputs: bool
    :param kwargs_to_upstream: For certain operators, we might need to upstream certain arguments
        that would otherwise be absorbed by the DecoratedOperator (for example python_callable for the
        PythonOperator). This gives a user the option to upstream kwargs as needed.
    :type kwargs_to_upstream: dict
    """

    template_fields = ('op_args', 'op_kwargs')
    template_fields_renderers = {"op_args": "py", "op_kwargs": "py"}

    # since we won't mutate the arguments, we should just do the shallow copy
    # there are some cases we can't deepcopy the objects (e.g protobuf).
    shallow_copy_attrs = ('python_callable',)

    def __init__(
        self,
        *,
        python_callable: Callable,
        task_id: str,
        op_args: Tuple[Any],
        op_kwargs: Dict[str, Any],
        multiple_outputs: bool = False,
        kwargs_to_upstream: dict = None,
        **kwargs,
    ) -> None:
        kwargs['task_id'] = get_unique_task_id(task_id, kwargs.get('dag'), kwargs.get('task_group'))
        self.python_callable = python_callable
        kwargs_to_upstream = kwargs_to_upstream or {}

        # Check that arguments can be binded
        signature(python_callable).bind(*op_args, **op_kwargs)
        self.multiple_outputs = multiple_outputs
        self.op_args = op_args
        self.op_kwargs = op_kwargs
        super().__init__(**kwargs_to_upstream, **kwargs)

    def execute(self, context: Dict):
        return_value = super().execute(context)
        self._handle_output(return_value=return_value, context=context, xcom_push=self.xcom_push)
        return return_value

    def _handle_output(self, return_value: Any, context: Dict, xcom_push: Callable):
        """
        Handles logic for whether a decorator needs to push a single return value or multiple return values.

        :param return_value:
        :param context:
        :param xcom_push:
        """
        if not self.multiple_outputs:
            return return_value
        if isinstance(return_value, dict):
            for key in return_value.keys():
                if not isinstance(key, str):
                    raise AirflowException(
                        'Returned dictionary keys must be strings when using '
                        f'multiple_outputs, found {key} ({type(key)}) instead'
                    )
            for key, value in return_value.items():
                xcom_push(context, key, value)
        else:
            raise AirflowException(
                f'Returned output was type {type(return_value)} expected dictionary for multiple_outputs'
            )
        return return_value

    def _hook_apply_defaults(self, *args, **kwargs):
        if 'python_callable' not in kwargs:
            return args, kwargs

        python_callable = kwargs['python_callable']
        default_args = kwargs.get('default_args') or {}
        op_kwargs = kwargs.get('op_kwargs') or {}
        f_sig = signature(python_callable)
        for arg in f_sig.parameters:
            if arg not in op_kwargs and arg in default_args:
                op_kwargs[arg] = default_args[arg]
        kwargs['op_kwargs'] = op_kwargs
        return args, kwargs


T = TypeVar("T", bound=Callable)  # pylint: disable=invalid-name


def task_decorator_factory(
    python_callable: Optional[Callable] = None,
    multiple_outputs: Optional[bool] = None,
    decorated_operator_class: BaseOperator = None,
    **kwargs,
) -> Callable[[T], T]:
    """
    A factory that generates a wrapper that raps a function into an Airflow operator.
    Accepts kwargs for operator kwarg. Can be reused in a single DAG.

    :param python_callable: Function to decorate
    :type python_callable: Optional[Callable]
    :param multiple_outputs: if set, function return value will be
        unrolled to multiple XCom values. List/Tuples will unroll to xcom values
        with index as key. Dict will unroll to xcom values with keys as XCom keys.
        Defaults to False.
    :type multiple_outputs: bool
    :param decorated_operator_class: The operator that executes the logic needed to run the python function in
        the correct environment
    :type decorated_operator_class: BaseDecoratedOperator

    """
    # try to infer from  type annotation
    if python_callable and multiple_outputs is None:
        sig = signature(python_callable).return_annotation
        ttype = getattr(sig, "__origin__", None)

        multiple_outputs = sig != inspect.Signature.empty and ttype in (dict, Dict)

    def wrapper(f: T):
        """
        Python wrapper to generate PythonDecoratedOperator out of simple python functions.
        Used for Airflow Decorated interface
        """
        validate_python_callable(f)
        kwargs.setdefault('task_id', f.__name__)

        @functools.wraps(f)
        def factory(*args, **f_kwargs):
            op = decorated_operator_class(
                python_callable=f,
                op_args=args,
                op_kwargs=f_kwargs,
                multiple_outputs=multiple_outputs,
                **kwargs,
            )
            if f.__doc__:
                op.doc_md = f.__doc__
            return XComArg(op)

        return cast(T, factory)

    if callable(python_callable):
        return wrapper(python_callable)
    elif python_callable is not None:
        raise AirflowException('No args allowed while using @task, use kwargs instead')
    return wrapper