from argparse import ArgumentParser
from collections import defaultdict
from pathlib2 import Path
from typing import Tuple
from itertools import chain

import yaml
from six.moves import input

from trains import Task
from trains.automation.aws_auto_scaler import AwsAutoScaler
from trains.config import running_remotely
from trains.utilities.wizard.user_input import get_input, input_int, input_bool

CONF_FILE = "aws_autoscaler.yaml"
DEFAULT_DOCKER_IMAGE = "nvidia/cuda:10.1-runtime-ubuntu18.04"


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "--run",
        help="Run the autoscaler after wizard finished",
        action="store_true",
        default=False,
    )
    parser.add_argument(
        "--remote",
        help="Run the autoscaler as a service, launch on the `services` queue",
        action="store_true",
        default=False,
    )
    args = parser.parse_args()

    if running_remotely():
        hyper_params = AwsAutoScaler.Settings().as_dict()
        configurations = AwsAutoScaler.Configuration().as_dict()
    else:
        print("AWS Autoscaler setup wizard\n"
              "---------------------------\n"
              "Follow the wizard to configure your AWS auto-scaler service.\n"
              "Once completed, you will be able to view and change the configuration in the trains-server web UI.\n"
              "It means there is no need to worry about typos or mistakes :)\n")

        config_file = Path(CONF_FILE).absolute()
        if config_file.exists() and input_bool(
            "Load configurations from config file '{}' [Y/n]? ".format(str(CONF_FILE)),
            default=True,
        ):
            with config_file.open("r") as f:
                conf = yaml.load(f, Loader=yaml.SafeLoader)
            hyper_params = conf["hyper_params"]
            configurations = conf["configurations"]
        else:
            configurations, hyper_params = run_wizard()

            # noinspection PyBroadException
            try:
                with config_file.open("w+") as f:
                    conf = {
                        "hyper_params": hyper_params,
                        "configurations": configurations,
                    }
                    yaml.safe_dump(conf, f)
            except Exception:
                print(
                    "Error! Could not write configuration file at: {}".format(
                        str(CONF_FILE)
                    )
                )
                return

    task = Task.init(project_name="DevOps", task_name="AWS Auto-Scaler", task_type=Task.TaskTypes.service)
    task.connect(hyper_params)
    task.connect_configuration(configurations)

    if args.remote or args.run:
        print("Running AWS auto-scaler as a service\nExecution log {}".format(task.get_output_log_web_page()))

    if args.remote:
        # if we are running remotely enqueue this run, and leave the process
        # the trains-agent services will pick it up and execute it for us.
        task.execute_remotely(queue_name='services')

    autoscaler = AwsAutoScaler(hyper_params, configurations)
    if running_remotely() or args.run:
        autoscaler.start()


def run_wizard():
    # type: () -> Tuple[dict, dict]

    hyper_params = AwsAutoScaler.Settings()
    configurations = AwsAutoScaler.Configuration()

    hyper_params.cloud_credentials_key = get_input("AWS Access Key ID", required=True)
    hyper_params.cloud_credentials_secret = get_input(
        "AWS Secret Access Key", required=True
    )
    hyper_params.cloud_credentials_region = get_input(
        "AWS region name",
        "[us-east-1b]",
        default='us-east-1b')
    # get GIT User/Pass for cloning
    print(
        "\nGIT credentials:"
        "\nEnter GIT username for repository cloning (leave blank for SSH key authentication): [] ",
        end="",
    )
    git_user = input()
    if git_user.strip():
        print("Enter password for user '{}': ".format(git_user), end="")
        git_pass = input()
        print(
            "Git repository cloning will be using user={} password={}".format(
                git_user, git_pass
            )
        )
    else:
        git_user = None
        git_pass = None

    hyper_params.git_user = git_user
    hyper_params.git_pass = git_pass

    hyper_params.default_docker_image = get_input(
        "default docker image/parameters",
        "to use [{}]".format(DEFAULT_DOCKER_IMAGE),
        default=DEFAULT_DOCKER_IMAGE,
        new_line=True,
    )
    print("\nConfigure the machine types for the auto-scaler:")
    print("------------------------------------------------")
    resource_configurations = {}
    while True:
        a_resource = {
            "instance_type": get_input(
                "Amazon instance type",
                "['g4dn.4xlarge']",
                question='Select',
                default="g4dn.4xlarge",
            ),
            "is_spot": input_bool(
                "Use spot instances? [y/N]"
            ),
            "availability_zone": get_input(
                "availability zone",
                "['us-east-1b']",
                question='Select',
                default="us-east-1b",
            ),
            "ami_id": get_input(
                "the Amazon Machine Image id",
                "['ami-07c95cafbb788face']",
                question='Select',
                default="ami-07c95cafbb788face",
            ),
            "ebs_device_name": get_input(
                "the Amazon EBS device",
                "['/dev/xvda']",
                default="/dev/xvda",
            ),
            "ebs_volume_size": input_int(
                "the Amazon EBS volume size",
                "(in GiB) [100]",
                default=100,
            ),
            "ebs_volume_type": get_input(
                "the Amazon EBS volume type",
                "['gp2']",
                default="gp2",
            ),
        }

        while True:
            resource_name = get_input(
                "a name for this instance type",
                "(used in the budget section) For example 'aws4gpu'",
                question='Select',
                required=True,
            )
            if resource_name in resource_configurations:
                print("\tError: instance type '{}' already used!".format(resource_name))
                continue
            break
        resource_configurations[resource_name] = a_resource

        if not input_bool("\nDefine another instance type? [y/N]"):
            break

    configurations.resource_configurations = resource_configurations

    configurations.extra_vm_bash_script = input(
        "\nEnter any pre-execution bash script to be executed on the newly created instances []: "
    )

    print("\nDefine the machines budget:")
    print("-----------------------------")
    resource_configurations_names = list(configurations.resource_configurations.keys())
    queues = defaultdict(list)
    while True:
        while True:
            queue_name = get_input("a queue name (for example: 'aws_4gpu_machines')", question='Select', required=True)
            if queue_name in queues:
                print("\tError: queue name '{}' already used!".format(queue_name))
                continue
            break

        while True:
            valid_instances = [k for k in resource_configurations_names
                               if k not in (q[0] for q in queues[queue_name])]
            while True:
                queue_type = get_input(
                    "a instance type to attach to the queue",
                    "{}".format(valid_instances),
                    question="Select",
                    required=True,
                )
                if queue_type not in configurations.resource_configurations:
                    print("\tError: instance type '{}' not in predefined instances {}!".format(
                        queue_type, list(configurations.resource_configurations.keys())))
                    continue

                if queue_type in (q[0] for q in queues[queue_name]):
                    print("\tError: instance type '{}' already in {}!".format(
                        queue_type, queue_name))
                    continue

                if queue_type in [q[0] for q in chain.from_iterable(queues.values())]:
                    queue_type_new = '{}_{}'.format(queue_type, queue_name)
                    print("\tInstance type '{}' already used, renaming instance to {}".format(
                        queue_type, queue_type_new))
                    configurations.resource_configurations[queue_type_new] = \
                        dict(**configurations.resource_configurations[queue_type])
                    queue_type = queue_type_new

                    # make sure the renamed name is not reused
                    if queue_type in (q[0] for q in queues[queue_name]):
                        print("\tError: instance type '{}' already in {}!".format(
                            queue_type, queue_name))
                        continue

                break
            max_instances = input_int(
                "maximum number of '{}' instances to spin simultaneously (example: 3)".format(queue_type),
                required=True
            )

            queues[queue_name].append((queue_type, max_instances))
            valid_instances = [k for k in configurations.resource_configurations.keys()
                               if k not in (q[0] for q in queues[queue_name])]
            if not valid_instances:
                break

            if not input_bool("Do you wish to add another instance type to queue? [y/N]: "):
                break
        if not input_bool("\nAdd another queue? [y/N]: "):
            break
    configurations.queues = dict(queues)

    hyper_params.max_idle_time_min = input_int(
        "maximum idle time",
        "for the auto-scaler to spin down an instance (in minutes) [15]",
        default=15,
        new_line=True,
    )
    hyper_params.polling_interval_time_min = input_int(
        "instances polling interval", "for the auto-scaler (in minutes) [5]", default=5,
    )

    return configurations.as_dict(), hyper_params.as_dict()


if __name__ == "__main__":
    main()
