import functools
from typing import Any, Callable, Dict, Tuple, Union

from flyteidl.core import tasks_pb2 as _core_task
from google.protobuf.json_format import MessageToDict

from flytekit.annotated import task
from flytekit.annotated.context_manager import (
    BranchEvalMode,
    ExecutionState,
    FlyteContext,
    FlyteEntities,
    RegistrationSettings,
)
from flytekit.annotated.promise import Promise
from flytekit.annotated.python_function_task import PythonFunctionTask
from flytekit.annotated.task import TaskPlugins
from flytekit.annotated.workflow import Workflow
from flytekit.common.exceptions import user as _user_exceptions
from flytekit.loggers import logger
from flytekit.models import dynamic_job as _dynamic_job
from flytekit.models import literals as _literal_models
from flytekit.models import task as _task_model
from flytekit.models import task as _task_models
from flytekit.plugins import k8s as _lazy_k8s


class Sidecar(object):
    def __init__(self, pod_spec: _lazy_k8s.io.api.core.v1.generated_pb2.PodSpec, primary_container_name: str):
        if not pod_spec:
            raise _user_exceptions.FlyteValidationException("A pod spec cannot be undefined")
        if not primary_container_name:
            raise _user_exceptions.FlyteValidationException("A primary container name cannot be undefined")

        self._pod_spec = pod_spec
        self._primary_container_name = primary_container_name

    @property
    def pod_spec(self) -> _lazy_k8s.io.api.core.v1.generated_pb2.PodSpec:
        return self._pod_spec

    @property
    def primary_container_name(self) -> str:
        return self._primary_container_name


class SidecarFunctionTask(PythonFunctionTask[Sidecar]):
    def __init__(
        self, task_config: Sidecar, task_function: Callable, metadata: _task_model.TaskMetadata, *args, **kwargs
    ):
        super(SidecarFunctionTask, self).__init__(
            task_config=task_config,
            task_type="sidecar",
            task_function=task_function,
            metadata=metadata,
            *args,
            **kwargs,
        )

    def get_custom(self, settings: RegistrationSettings) -> Dict[str, Any]:
        containers = self.task_config.pod_spec.containers
        primary_exists = False
        for container in containers:
            if container.name == self.task_config.primary_container_name:
                primary_exists = True
                break
        if not primary_exists:
            # insert a placeholder primary container if it is not defined in the pod spec.
            containers.extend(
                [_lazy_k8s.io.api.core.v1.generated_pb2.Container(name=self.task_config.primary_container_name)]
            )

        final_containers = []
        for container in containers:
            # In the case of the primary container, we overwrite specific container attributes with the default values
            # used in an SDK runnable task.
            if container.name == self.task_config.primary_container_name:
                sdk_default_container = self.get_container(settings)

                container.image = sdk_default_container.image
                # clear existing commands
                del container.command[:]
                container.command.extend(sdk_default_container.command)
                # also clear existing args
                del container.args[:]
                container.args.extend(sdk_default_container.args)

                resource_requirements = _lazy_k8s.io.api.core.v1.generated_pb2.ResourceRequirements()
                for resource in sdk_default_container.resources.limits:
                    resource_requirements.limits[
                        _core_task.Resources.ResourceName.Name(resource.name).lower()
                    ].CopyFrom(_lazy_k8s.io.apimachinery.pkg.api.resource.generated_pb2.Quantity(string=resource.value))
                for resource in sdk_default_container.resources.requests:
                    resource_requirements.requests[
                        _core_task.Resources.ResourceName.Name(resource.name).lower()
                    ].CopyFrom(_lazy_k8s.io.apimachinery.pkg.api.resource.generated_pb2.Quantity(string=resource.value))
                if resource_requirements.ByteSize():
                    # Important! Only copy over resource requirements if they are non-empty.
                    container.resources.CopyFrom(resource_requirements)

                del container.env[:]
                container.env.extend(
                    [
                        _lazy_k8s.io.api.core.v1.generated_pb2.EnvVar(name=key, value=val)
                        for key, val in sdk_default_container.env.items()
                    ]
                )

            final_containers.append(container)

        del self.task_config._pod_spec.containers[:]
        self.task_config._pod_spec.containers.extend(final_containers)

        sidecar_job_plugin = _task_models.SidecarJob(
            pod_spec=self.task_config.pod_spec, primary_container_name=self.task_config.primary_container_name,
        ).to_flyte_idl()
        return MessageToDict(sidecar_job_plugin)

    def _local_execute(self, ctx: FlyteContext, **kwargs) -> Union[Tuple[Promise], Promise, None]:
        raise _user_exceptions.FlyteUserException("Local execute is not currently supported for sidecar tasks")


TaskPlugins.register_pythontask_plugin(Sidecar, SidecarFunctionTask)
