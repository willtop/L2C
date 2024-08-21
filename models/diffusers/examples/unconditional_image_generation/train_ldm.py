import argparse
import inspect
import logging
import math
import os
from pathlib import Path
from typing import Optional

import accelerate
import datasets
import torch
import torch.nn.functional as F
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import ProjectConfiguration
from datasets import load_dataset
from huggingface_hub import HfFolder, Repository, create_repo, whoami
from packaging import version
from torchvision import transforms
from tqdm.auto import tqdm

import diffusers
from diffusers import DDPMPipeline, DDPMScheduler, UNet2DModel, DDIMPipeline, DDIMScheduler
from diffusers.optimization import get_scheduler
from diffusers.training_utils import EMAModel
from diffusers.utils import check_min_version, is_tensorboard_available, is_wandb_available
from diffusers.models import AutoencoderKL
from diffusers.utils import randn_tensor

# library from object centric lib
from tqdm import tqdm
from obj_cen_data import datasets as mo_datasets


# Will error if the minimal version of diffusers is not installed. Remove at your own risks.
check_min_version("0.14.0.dev0")

logger = get_logger(__name__, log_level="INFO")


def _extract_into_tensor(arr, timesteps, broadcast_shape):
    """
    Extract values from a 1-D numpy array for a batch of indices.

    :param arr: the 1-D numpy array.
    :param timesteps: a tensor of indices into the array to extract.
    :param broadcast_shape: a larger shape of K dimensions with the batch
                            dimension equal to the length of timesteps.
    :return: a tensor of shape [batch_size, 1, ...] where the shape has K dims.
    """
    if not isinstance(arr, torch.Tensor):
        arr = torch.from_numpy(arr)
    res = arr[timesteps].float().to(timesteps.device)
    while len(res.shape) < len(broadcast_shape):
        res = res[..., None]
    return res.expand(broadcast_shape)


def parse_args():
    parser = argparse.ArgumentParser(description="Simple example of a training script.")
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help=(
            "The name of the Dataset (from the HuggingFace hub) to train on (could be your own, possibly private,"
            " dataset). It can also be a path pointing to a local copy of a dataset in your filesystem,"
            " or to a folder containing files that HF Datasets can understand."
        ),
    )

    parser.add_argument("--dataset_mo", action="store_true")
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The config of the Dataset, leave as None if there's only one config.",
    )
    parser.add_argument(
        "--model_config_name_or_path",
        type=str,
        default=None,
        help="The config of the UNet model to train, leave as None to use standard DDPM configuration.",
    )
    parser.add_argument(
        "--train_data_dir",
        type=str,
        default=None,
        help=(
            "A folder containing the training data. Folder contents must follow the structure described in"
            " https://huggingface.co/docs/datasets/image_dataset#imagefolder. In particular, a `metadata.jsonl` file"
            " must exist to provide the captions for the images. Ignored if `dataset_name` is specified."
        ),
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default="ddpm-model-64",
        help="The output directory where the model predictions and checkpoints will be written.",
    )
    parser.add_argument("--overwrite_output_dir", action="store_true")
    parser.add_argument(
        "--cache_dir",
        type=str,
        default=None,
        help="The directory where the downloaded models and datasets will be stored.",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=128,
        help=(
            "The resolution for input images, all the images in the train/validation dataset will be resized to this"
            " resolution"
        ),
    )
    parser.add_argument(
        "--center_crop",
        default=False,
        action="store_true",
        help=(
            "Whether to center crop the input images to the resolution. If not set, the images will be randomly"
            " cropped. The images will be resized to the resolution first before cropping."
        ),
    )
    parser.add_argument(
        "--random_flip",
        default=False,
        action="store_true",
        help="whether to randomly flip images horizontally",
    )
    parser.add_argument(
        "--train_batch_size", type=int, default=16, help="Batch size (per device) for the training dataloader."
    )
    parser.add_argument(
        "--eval_batch_size", type=int, default=16, help="The number of images to generate for evaluation."
    )
    parser.add_argument(
        "--dataloader_num_workers",
        type=int,
        default=8,
        help=(
            "The number of subprocesses to use for data loading. 0 means that the data will be loaded in the main"
            " process."
        ),
    )
    parser.add_argument("--num_epochs", type=int, default=100)
    parser.add_argument("--save_images_epochs", type=int, default=5, help="How often to save images during training.")
    parser.add_argument(
        "--save_model_epochs", type=int, default=10, help="How often to save the model during training."
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=1e-4,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument(
        "--lr_scheduler",
        type=str,
        default="cosine",
        help=(
            'The scheduler type to use. Choose between ["linear", "cosine", "cosine_with_restarts", "polynomial",'
            ' "constant", "constant_with_warmup"]'
        ),
    )
    parser.add_argument(
        "--lr_warmup_steps", type=int, default=500, help="Number of steps for the warmup in the lr scheduler."
    )
    parser.add_argument("--adam_beta1", type=float, default=0.95, help="The beta1 parameter for the Adam optimizer.")
    parser.add_argument("--adam_beta2", type=float, default=0.999, help="The beta2 parameter for the Adam optimizer.")
    parser.add_argument(
        "--adam_weight_decay", type=float, default=1e-6, help="Weight decay magnitude for the Adam optimizer."
    )
    parser.add_argument("--adam_epsilon", type=float, default=1e-08, help="Epsilon value for the Adam optimizer.")
    parser.add_argument(
        "--use_ema",
        action="store_true",
        help="Whether to use Exponential Moving Average for the final model weights.",
    )
    parser.add_argument("--ema_inv_gamma", type=float, default=1.0, help="The inverse gamma value for the EMA decay.")
    parser.add_argument("--ema_power", type=float, default=3 / 4, help="The power value for the EMA decay.")
    parser.add_argument("--ema_max_decay", type=float, default=0.9999, help="The maximum decay magnitude for EMA.")
    parser.add_argument("--push_to_hub", action="store_true", help="Whether or not to push the model to the Hub.")
    parser.add_argument("--hub_token", type=str, default=None, help="The token to use to push to the Model Hub.")
    parser.add_argument(
        "--hub_model_id",
        type=str,
        default=None,
        help="The name of the repository to keep in sync with the local `output_dir`.",
    )
    parser.add_argument(
        "--hub_private_repo", action="store_true", help="Whether or not to create a private repository."
    )
    parser.add_argument(
        "--logger",
        type=str,
        default="tensorboard",
        choices=["tensorboard", "wandb"],
        help=(
            "Whether to use [tensorboard](https://www.tensorflow.org/tensorboard) or [wandb](https://www.wandb.ai)"
            " for experiment tracking and logging of model metrics and model checkpoints"
        ),
    )
    parser.add_argument(
        "--logging_dir",
        type=str,
        default="logs",
        help=(
            "[TensorBoard](https://www.tensorflow.org/tensorboard) log directory. Will default to"
            " *output_dir/runs/**CURRENT_DATETIME_HOSTNAME***."
        ),
    )
    parser.add_argument("--local_rank", type=int, default=-1, help="For distributed training: local_rank")
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default="no",
        choices=["no", "fp16", "bf16"],
        help=(
            "Whether to use mixed precision. Choose"
            "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
            "and an Nvidia Ampere GPU."
        ),
    )
    parser.add_argument(
        "--prediction_type",
        type=str,
        default="epsilon",
        choices=["epsilon", "sample"],
        help="Whether the model should predict the 'epsilon'/noise error or directly the reconstructed image 'x0'.",
    )
    parser.add_argument("--ddpm_num_steps", type=int, default=1000)
    parser.add_argument("--ddpm_num_inference_steps", type=int, default=1000)
    parser.add_argument("--ddpm_beta_schedule", type=str, default="linear")
    parser.add_argument(
        "--checkpointing_steps",
        type=int,
        default=500,
        help=(
            "Save a checkpoint of the training state every X updates. These checkpoints are only suitable for resuming"
            " training using `--resume_from_checkpoint`."
        ),
    )
    parser.add_argument(
        "--checkpoints_total_limit",
        type=int,
        default=None,
        help=(
            "Max number of checkpoints to store. Passed as `total_limit` to the `Accelerator` `ProjectConfiguration`."
            " See Accelerator::save_state https://huggingface.co/docs/accelerate/package_reference/accelerator#accelerate.Accelerator.save_state"
            " for more docs"
        ),
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help=(
            "Whether training should be resumed from a previous checkpoint. Use a path saved by"
            ' `--checkpointing_steps`, or `"latest"` to automatically select the last available checkpoint.'
        ),
    )

    args = parser.parse_args()
    env_local_rank = int(os.environ.get("LOCAL_RANK", -1))
    if env_local_rank != -1 and env_local_rank != args.local_rank:
        args.local_rank = env_local_rank

    if args.dataset_name is None and args.train_data_dir is None:
        raise ValueError("You must specify either a dataset name from the hub or a train data directory.")

    return args


def get_full_repo_name(model_id: str, organization: Optional[str] = None, token: Optional[str] = None):
    if token is None:
        token = HfFolder.get_token()
    if organization is None:
        username = whoami(token)["name"]
        return f"{username}/{model_id}"
    else:
        return f"{organization}/{model_id}"


def main(args):
    logging_dir = os.path.join(args.output_dir, args.logging_dir)

    accelerator_project_config = ProjectConfiguration(total_limit=args.checkpoints_total_limit)

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.logger,
        logging_dir=logging_dir,
        project_config=accelerator_project_config,
    )

    if args.logger == "tensorboard":
        if not is_tensorboard_available():
            raise ImportError("Make sure to install tensorboard if you want to use it for logging during training.")

    elif args.logger == "wandb":
        if not is_wandb_available():
            raise ImportError("Make sure to install wandb if you want to use it for logging during training.")
        import wandb

    # `accelerate` 0.16.0 will have better support for customized saving
    if version.parse(accelerate.__version__) >= version.parse("0.16.0"):
        # create custom saving & loading hooks so that `accelerator.save_state(...)` serializes in a nice format
        def save_model_hook(models, weights, output_dir):
            if args.use_ema:
                ema_model.save_pretrained(os.path.join(output_dir, "unet_ema"))

            for i, model in enumerate(models):
                model.save_pretrained(os.path.join(output_dir, "unet"))

                # make sure to pop weight so that corresponding model is not saved again
                weights.pop()

        def load_model_hook(models, input_dir):
            if args.use_ema:
                load_model = EMAModel.from_pretrained(os.path.join(input_dir, "unet_ema"), UNet2DModel)
                ema_model.load_state_dict(load_model.state_dict())
                ema_model.to(accelerator.device)
                del load_model

            for i in range(len(models)):
                # pop models so that they are not loaded again
                model = models.pop()

                # load diffusers style into model
                load_model = UNet2DModel.from_pretrained(input_dir, subfolder="unet")
                model.register_to_config(**load_model.config)

                model.load_state_dict(load_model.state_dict())
                del load_model

        accelerator.register_save_state_pre_hook(save_model_hook)
        accelerator.register_load_state_pre_hook(load_model_hook)

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )
    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        diffusers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        diffusers.utils.logging.set_verbosity_error()

    # Handle the repository creation
    if accelerator.is_main_process:
        if args.push_to_hub:
            if args.hub_model_id is None:
                repo_name = get_full_repo_name(Path(args.output_dir).name, token=args.hub_token)
            else:
                repo_name = args.hub_model_id
            create_repo(repo_name, exist_ok=True, token=args.hub_token)
            repo = Repository(args.output_dir, clone_from=repo_name, token=args.hub_token)

            with open(os.path.join(args.output_dir, ".gitignore"), "w+") as gitignore:
                if "step_*" not in gitignore:
                    gitignore.write("step_*\n")
                if "epoch_*" not in gitignore:
                    gitignore.write("epoch_*\n")
        elif args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    # Initialize the model
    if args.model_config_name_or_path is None:

        ddim_sampler = False

        ## config ptr large  ##
        # model = AEUNet2DModel(
        model = UNet2DModel(
            sample_size=args.resolution//8,
            in_channels=4,
            out_channels=4,
            layers_per_block=2,
            # block_out_channels=(32, 32, 32),
            block_out_channels=(192*2, 192*4, 192*8, 192*8),
            # block_out_channels=(224, 224*2, 224*3, 224*4),
            attention_head_dim=24,
            down_block_types=(
                "AttnResnetDownBlock2D",
                # "DownBlock2D",
                # "DownBlock2D",
                # "DownBlock2D",
                "AttnResnetDownBlock2D",
                "AttnResnetDownBlock2D",
                "AttnResnetDownBlock2D",
                # "AttnDownBlock2D",
            ),
            up_block_types=(
                # "UpBlock2D",
                # "UpBlock2D",
                # "UpBlock2D",
                "AttnResnetUpBlock2D",
                "AttnResnetUpBlock2D",
                "AttnResnetUpBlock2D",
                "AttnResnetUpBlock2D",
                # "AttnUpBlock2D",
                ),

            resnet_time_scale_shift='scale_shift',
        )



        # ## config ptr small  ##
        # # model = AEUNet2DModel(
        # model = UNet2DModel(
        #     sample_size=args.resolution//8,
        #     in_channels=4,
        #     out_channels=4,
        #     layers_per_block=2,
        #     block_out_channels=(224*1, 224*2, 224*4, 224*4),
        #     attention_head_dim=16,
        #     down_block_types=(
        #         "DownBlock2D", 
        #         "DownBlock2D", 
        #         "AttnDownBlock2D",
        #         "DownBlock2D", 
        #     ),
        #     up_block_types=(
        #         "UpBlock2D",
        #         "AttnUpBlock2D",
        #         "UpBlock2D",
        #         "UpBlock2D",
        #         ),
        # )

        # ## config ptr ##
        # model = UNet2DModel(
        #     sample_size=args.resolution//8,
        #     in_channels=4,
        #     out_channels=4,
        #     layers_per_block=2,
        #     block_out_channels=(224*1, 224*2, 224*4, 224*4),
        #     attention_head_dim=32,
        #     down_block_types=(
        #         "AttnDownBlock2D",
        #         "AttnDownBlock2D",
        #         "AttnDownBlock2D",
        #         "AttnDownBlock2D",
        #         # "DownBlock2D", 
        #         # "DownBlock2D", 
        #         # "AttnDownBlock2D",
        #         # "DownBlock2D", 
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #         # "DownBlock2D", 
        #     ),
        #     up_block_types=(

        #         "AttnUpBlock2D",
        #         "AttnUpBlock2D",
        #         "AttnUpBlock2D",
        #         "AttnUpBlock2D",
        #         # "UpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "UpBlock2D",
        #         # "UpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "UpBlock2D",
        #     ),
        # )

        # ## config 7 ##
        # model = UNet2DModel(
        #     sample_size=args.resolution//8,
        #     in_channels=4,
        #     out_channels=4,
        #     layers_per_block=2,
        #     block_out_channels=(192*1, 192*2, 192*4),
        #     attention_head_dim=16,
        #     down_block_types=(
        #         "DownBlock2D", 
        #         "AttnDownBlock2D",
        #         "DownBlock2D", 
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #     ),
        #     up_block_types=(
        #         "UpBlock2D",
        #         "AttnUpBlock2D",
        #         "UpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "AttnUpBlock2D",
        #     ),
        #     resnet_time_scale_shift='scale_shift',
        # )

        # ## config-skip ##
        # model = UNet2DModel(
        #     sample_size=args.resolution//8,
        #     in_channels=4,
        #     out_channels=4,
        #     layers_per_block=2,
        #     block_out_channels=(224*1, 224*2, 224*4, 224*4),
        #     attention_head_dim=32,
        #     down_block_types=(
        #         "AttnResnetDownBlock2D",
        #         "AttnResnetDownBlock2D",
        #         "AttnResnetDownBlock2D",
        #         "AttnResnetDownBlock2D",
        #     ),
        #     up_block_types=(
        #         "AttnResnetUpBlock2D",
        #         "AttnResnetUpBlock2D",
        #         "AttnResnetUpBlock2D",
        #         "AttnResnetUpBlock2D",
        #     ),
        #     resnet_time_scale_shift='scale_shift',
        # )

        # ## config-256 ##
        # model = UNet2DModel(
        #     sample_size=args.resolution//8,
        #     in_channels=4,
        #     out_channels=4,
        #     layers_per_block=2,
        #     block_out_channels=(192*1, 192*2, 192*4, 192*4),
        #     attention_head_dim=32,
        #     down_block_types=(
        #         "DownBlock2D", 
        #         "DownBlock2D", 
        #         "AttnDownBlock2D",
        #         "DownBlock2D", 
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #         # "AttnDownBlock2D",
        #     ),
        #     up_block_types=(
        #         "UpBlock2D",
        #         "UpBlock2D",
        #         "AttnUpBlock2D",
        #         "UpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "AttnUpBlock2D",
        #         # "AttnUpBlock2D",
        #     ),
        #     # resnet_time_scale_shift='scale_shift',
        # )

        vae = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-ema")

        # freeze vae
        vae.requires_grad_(False)
        #vae = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-mse")

    else:
        config = UNet2DModel.load_config(args.model_config_name_or_path)
        model = UNet2DModel.from_config(config)

    # Create EMA for the model.
    if args.use_ema:
        ema_model = EMAModel(
            model.parameters(),
            decay=args.ema_max_decay,
            use_ema_warmup=True,
            inv_gamma=args.ema_inv_gamma,
            power=args.ema_power,
            model_cls=UNet2DModel,
            model_config=model.config,
        )

    # Initialize the scheduler
    # following LDM in LSUN-Church
    # beta_start = 0.00085
    # beta_end = 0.012

    # beta_start = 0.0015
    # beta_end = 0.0155

    # beta_start = 0.0015
    # beta_end = 0.0195

    beta_start = 0.0001
    beta_end = 0.02

    accepts_prediction_type = "prediction_type" in set(inspect.signature(DDPMScheduler.__init__).parameters.keys())
    if accepts_prediction_type:
        noise_scheduler = DDPMScheduler(
            beta_start=beta_start,
            beta_end=beta_end,
            num_train_timesteps=args.ddpm_num_steps,
            beta_schedule=args.ddpm_beta_schedule,
            prediction_type=args.prediction_type,
        )
        if ddim_sampler:
            test_noise_scheduler = DDIMScheduler(
                beta_start=beta_start,
                beta_end=beta_end,
                num_train_timesteps=args.ddpm_num_steps,
                beta_schedule=args.ddpm_beta_schedule,
                prediction_type=args.prediction_type,
            )
        else:
            test_noise_scheduler = noise_scheduler

    else:
        noise_scheduler = DDPMScheduler(
                beta_start=beta_start,
                beta_end=beta_end,
                num_train_timesteps=args.ddpm_num_steps, beta_schedule=args.ddpm_beta_schedule)
        if test_noise_scheduler:
            test_noise_scheduler = DDIMScheduler(
                beta_start=beta_start,
                beta_end=beta_end,
                num_train_timesteps=args.ddpm_num_steps, beta_schedule=args.ddpm_beta_schedule)
        else:
            test_noise_scheduler = noise_scheduler


    # Initialize the optimizer
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=args.learning_rate,
        # betas=(args.adam_beta1, args.adam_beta2),
        # weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).

    # Preprocessing the datasets and DataLoaders creation.
    if args.dataset_mo:
        augmentations = transforms.Compose(
            [
                # transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR),
                # transforms.CenterCrop(args.resolution) if args.center_crop else transforms.RandomCrop(args.resolution),
                # transforms.RandomResizedCrop(args.resolution, scale=(0.25, 1.0), ratio=(0.9, 1.1)),

                # transforms.CenterCrop(240),
                # transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR),
                transforms.RandomHorizontalFlip() if args.random_flip else transforms.Lambda(lambda x: x),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ])
    else:
        augmentations = transforms.Compose(
            [
                # transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR),
                # transforms.CenterCrop(args.resolution) if args.center_crop else transforms.RandomCrop(args.resolution),
                # transforms.RandomResizedCrop(args.resolution, scale=(0.25, 1.0), ratio=(0.9, 1.1)),

                # transforms.CenterCrop(240),
                # transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR),

                transforms.RandomHorizontalFlip() if args.random_flip else transforms.Lambda(lambda x: x),
                transforms.ToTensor(),
                transforms.Normalize([0.5], [0.5]),
            ]
    )


    # In distributed training, the load_dataset function guarantees that only one local process can concurrently
    # download the dataset.
    if args.dataset_name is not None:
        if args.dataset_mo:
            if 'multid' in args.dataset_name:
                dataset = mo_datasets.Multidsprites(
                    name='multid',
                    width=64,
                    height=64,
                    max_num_objects=6,
                    num_background_objects=1,
                    input_channels=3,
                    dataset_size=90000,
                    dataset_path=args.dataset_name,
                    output_features='all',
                    transform = augmentations,
                    )
            elif 'clevr' in args.dataset_name:
                if 'clevrtex' in args.dataset_name:
                    dataset_size=40000
                  
                    dataset = mo_datasets.ClevrTex(
                        name='clevrtex',
                        width=128,
                        height=128,
                        max_num_objects=11,
                        num_background_objects=1,
                        input_channels=3,
                        dataset_size=dataset_size,
                        dataset_path=args.dataset_name,
                        output_features='all',
                        transform = augmentations,
                        )

                else:
                    dataset_size=10000
                    # dataset_size=256

                    dataset = mo_datasets.Clevr(
                        name='clevr',
                        width=128,
                        height=128,
                        max_num_objects=11,
                        num_background_objects=1,
                        input_channels=3,
                        dataset_size=dataset_size,
                        dataset_path=args.dataset_name,
                        output_features='all',
                        transform = augmentations,
                        )
            elif 'movi-e' in args.dataset_name or 'movi-c' in args.dataset_name:
                dataset = mo_datasets.MOVI_E(
                    split='train',
                    root = args.dataset_name,
                    img_size=128,
                    num_segs=23,
                    transform = augmentations,
                    )
            elif 'msn-easy' in args.dataset_name:
                augmentations = transforms.Compose(
                    [
                        transforms.CenterCrop(240),
                        transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR),

                        transforms.RandomHorizontalFlip() if args.random_flip else transforms.Lambda(lambda x: x),
                        transforms.ToTensor(),
                        transforms.Normalize([0.5], [0.5]),
                    ])

                dataset = mo_datasets.MSN_Easy(
                    split='train',
                    root = args.dataset_name,
                    img_size=128,
                    num_segs=5,
                    transform = augmentations,
                    )

            elif 'PTR' in args.dataset_name:
                augmentations = transforms.Compose(
                    [
                        transforms.CenterCrop(600),
                        transforms.Resize(args.resolution, interpolation=transforms.InterpolationMode.BILINEAR),

                        transforms.RandomHorizontalFlip() if args.random_flip else transforms.Lambda(lambda x: x),
                        transforms.ToTensor(),
                        transforms.Normalize([0.5], [0.5]),
                    ])

                dataset = mo_datasets.PTR(
                    split='train',
                    root = args.dataset_name,
                    img_size=128,
                    num_segs=0,
                    transform = augmentations,
                    )

            else:
                a=1

        else:
            dataset = load_dataset(
                args.dataset_name,
                args.dataset_config_name,
                cache_dir=args.cache_dir,
                split="train",
            )

    else:
        dataset = load_dataset("imagefolder", data_dir=args.train_data_dir, cache_dir=args.cache_dir, split="train")
        # See more about loading custom images at
        # https://huggingface.co/docs/datasets/v2.4.0/en/image_load#imagefolder


    def transform_images(examples):
        images = [augmentations(image.convert("RGB")) for image in examples["image"]]
        return {"input": images}

    logger.info(f"Dataset size: {len(dataset)}")

    if not args.dataset_mo:
        dataset.set_transform(transform_images)
    train_dataloader = torch.utils.data.DataLoader(
        dataset, batch_size=args.train_batch_size, shuffle=True, num_workers=args.dataloader_num_workers, 
        # pin_memory=True,
    )


    # Initialize the learning rate scheduler
    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=(len(train_dataloader) * args.num_epochs),
    )

    # Prepare everything with our `accelerator`.
    # model, vae, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
    #     model, vae, optimizer, train_dataloader, lr_scheduler
    # )
    # model, vae, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
    #     model, vae, optimizer, train_dataloader, lr_scheduler
    # )

    model, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, lr_scheduler
    )

    vae.to(accelerator.device)
    if args.use_ema:
        ema_model.to(accelerator.device)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        run = os.path.split(__file__)[-1].split(".")[0]
        accelerator.init_trackers(run)

    total_batch_size = args.train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    max_train_steps = args.num_epochs * num_update_steps_per_epoch

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(dataset)}")
    logger.info(f"  Num Epochs = {args.num_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {max_train_steps}")

    global_step = 0
    first_epoch = 0

    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint != "latest":
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = os.listdir(args.output_dir)
            dirs = [d for d in dirs if d.startswith("checkpoint")]
            dirs = sorted(dirs, key=lambda x: int(x.split("-")[1]))
            path = dirs[-1] if len(dirs) > 0 else None

        if path is None:
            accelerator.print(
                f"Checkpoint '{args.resume_from_checkpoint}' does not exist. Starting a new training run."
            )
            args.resume_from_checkpoint = None
        else:
            path = args.resume_from_checkpoint
            accelerator.print(f"Resuming from checkpoint {path}")

            model.module.load_state_dict(torch.load(path))

            # accelerator.load_state(path)
            # accelerator.load_state(os.path.join(args.output_dir, path))
            # global_step = int(path.split("-")[1])

            # lets start from the beginning
            global_step = 0
            resume_global_step = global_step * args.gradient_accumulation_steps

            first_epoch = global_step // num_update_steps_per_epoch
            resume_step = resume_global_step % (num_update_steps_per_epoch * args.gradient_accumulation_steps)

    # Train!
    for epoch in range(first_epoch, args.num_epochs):
        model.train()
        vae.eval()

        progress_bar = tqdm(total=num_update_steps_per_epoch, disable=not accelerator.is_local_main_process)
        progress_bar.set_description(f"Epoch {epoch}")
        for step, batch in enumerate(train_dataloader):
            # Skip steps until we reach the resumed step
            if args.resume_from_checkpoint and epoch == first_epoch and step < resume_step:
                if step % args.gradient_accumulation_steps == 0:
                    progress_bar.update(1)
                continue

            clean_images = batch["input"]

            # import ipdb; ipdb.set_trace(context=15)
            # convert to latent (scaling factor is std measured from the first batch) 
            # scaling_factor = 0.18215
            scaling_factor = 0.18215 * 0.5

            with torch.no_grad():
                latents = vae.encode(clean_images).latent_dist.sample()
                    
                # rescaling the latents to be unit standard devication
                latents = latents * scaling_factor 

                ##### Diffusion training #####
                # Sample noise that we'll add to the images
                noise = torch.randn(latents.shape).to(latents.device)
                bsz = latents.shape[0]
                # Sample a random timestep for each image
                timesteps = torch.randint(
                    0, noise_scheduler.config.num_train_timesteps, (bsz,), device=latents.device
                ).long()

                # Add noise to the clean images according to the noise magnitude at each timestep
                # (this is the forward diffusion process)
                noisy_images = noise_scheduler.add_noise(latents, noise, timesteps)

            with accelerator.accumulate(model):
                # Predict the noise residual
                model_output = model(noisy_images, timesteps).sample

                if args.prediction_type == "epsilon":
                    # loss = F.mse_loss(model_output, noise)  # this could have different weights!


                    ###########################
                    # Add min-SNR-5 weighting
                    ###########################
                    new_weighting = False
                    if new_weighting:
                        alpha_t = _extract_into_tensor(
                            noise_scheduler.alphas_cumprod, timesteps, (latents.shape[0], 1, 1, 1)
                        )
                        snr_weights = alpha_t / (1 - alpha_t)
                        gamma = 5.0
                        final_weights = torch.clamp(gamma / snr_weights, max=1.0)
                        unweighted_loss = F.l1_loss(model_output, noise, reduction='none')
                        loss = unweighted_loss * final_weights
                        loss = loss.mean()
                        loss_log = unweighted_loss.mean()
                        loss_x_0 = (unweighted_loss / snr_weights).mean()
                    else:
                        alpha_t = _extract_into_tensor(
                            noise_scheduler.alphas_cumprod, timesteps, (latents.shape[0], 1, 1, 1)
                        )
                        snr_weights = alpha_t / (1 - alpha_t)

                        unweighted_loss = F.l1_loss(model_output, noise, reduction='none')
                        loss = unweighted_loss.mean()
                        loss_log = loss
                        loss_x_0 = (unweighted_loss / snr_weights).mean()

                elif args.prediction_type == "sample":
                    alpha_t = _extract_into_tensor(
                        noise_scheduler.alphas_cumprod, timesteps, (latents.shape[0], 1, 1, 1)
                    )
                    snr_weights = alpha_t / (1 - alpha_t)
                    loss = snr_weights * F.mse_loss(
                        model_output, latents, reduction="none"
                    )  # use SNR weighting from distillation paper
                    loss = loss.mean()
                else:
                    raise ValueError(f"Unsupported prediction type: {args.prediction_type}")

                accelerator.backward(loss)

                if accelerator.sync_gradients:
                    accelerator.clip_grad_norm_(model.parameters(), 1.0)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                if args.use_ema:
                    ema_model.step(model.parameters())
                progress_bar.update(1)
                global_step += 1

                # if global_step % args.checkpointing_steps == 0:
                #     if accelerator.is_main_process:
                #         save_path = os.path.join(args.output_dir, f"checkpoint-{global_step}")
                #         accelerator.save_state(save_path)
                #         logger.info(f"Saved state to {save_path}")

            logs = {"loss": loss_log.detach().item(), "loss_x_0": loss_x_0.detach().item() ,"lr": lr_scheduler.get_last_lr()[0], "step": global_step}
            #logs = {"loss": loss.detach().item(), "lr": lr_scheduler.get_last_lr()[0], "step": global_step}
            if args.use_ema:
                logs["ema_decay"] = ema_model.cur_decay_value
            progress_bar.set_postfix(**logs)

            # if global_step%5000==0:
            if global_step%1000==0:
                accelerator.log(logs, step=global_step)
        progress_bar.close()

        accelerator.wait_for_everyone()

        # Generate sample images for visual inspection
        if accelerator.is_main_process:
            if epoch % args.save_images_epochs == 0 or epoch == args.num_epochs - 1:

                with torch.no_grad():
                    unet = accelerator.unwrap_model(model)
                    if args.use_ema:
                        ema_model.copy_to(unet.parameters())

                    if ddim_sampler:
                        pipeline = DDIMPipeline(
                            unet=unet,
                            scheduler=test_noise_scheduler,
                        )

                    else:
                        pipeline = DDPMPipeline(
                            unet=unet,
                            scheduler=test_noise_scheduler,
                        )

                    generator = torch.Generator(device=pipeline.device).manual_seed(0)

                    ################## inference in latent space ########################
                    # Sample gaussian noise to begin loop
                    batch_size = 32
                    num_inference_steps=1000
                    if isinstance(unet.sample_size, int):
                        image_shape = (batch_size, unet.in_channels, unet.sample_size, unet.sample_size)
                    else:
                        image_shape = (batch_size, unet.in_channels, *unet.sample_size)

                    if pipeline.device.type == "mps":
                        # randn does not work reproducibly on mps
                        image = randn_tensor(image_shape, generator=generator)
                        image = image.to(pipeline.device)
                    else:
                        image = randn_tensor(image_shape, generator=generator, device=pipeline.device)

                    # set step values
                    test_noise_scheduler.set_timesteps(num_inference_steps)

                    for t in tqdm(test_noise_scheduler.timesteps):
                        # 1. predict noise model_output
                        model_output = unet(image, t).sample

                        # 2. compute previous image: x_t -> x_t-1
                        image = test_noise_scheduler.step(model_output, t, image, generator=generator).prev_sample
                    #####################################################################

                    # we need to convert it to pixel space.
                    # rescale the latent 
                    image = image / scaling_factor
                    recon = vae.decode(image).sample
                    
                    # denormalize 
                    final_images = ((recon + 1.0)/2).clamp(0, 1)
                    final_images = final_images.cpu().permute(0, 2, 3, 1).numpy()

                    # denormalize the images and save to tensorboard
                    images_processed = (final_images * 255).round().astype("uint8")


                    # vae recon
                    latents = latents / scaling_factor
                    vae_recon = vae.decode(latents).sample
                    vae_recon = ((vae_recon + 1.0)/2).clamp(0, 1)
                    vae_recon = vae_recon.cpu().permute(0, 2, 3, 1).numpy()
                    gt = (clean_images + 1.0)/2
                    gt = gt.cpu().permute(0, 2, 3, 1).numpy()

                if args.logger == "tensorboard":
                    accelerator.get_tracker("tensorboard").add_images(
                        "test_samples", images_processed.transpose(0, 3, 1, 2), epoch
                    )
                    accelerator.get_tracker("tensorboard").add_images(
                        "GT", gt.transpose(0, 3, 1, 2), epoch
                    )
                    accelerator.get_tracker("tensorboard").add_images(
                        "Recon", vae_recon.transpose(0, 3, 1, 2), epoch
                    )

                elif args.logger == "wandb":
                    accelerator.get_tracker("wandb").log(
                        {"test_samples": [wandb.Image(img) for img in images_processed], "epoch": epoch},
                        step=global_step,
                    )

            if epoch % args.save_model_epochs == 0 or epoch == args.num_epochs - 1:
                if accelerator.is_main_process:
                    save_path = os.path.join(args.output_dir, f"checkpoint-{epoch}")
                    accelerator.save_state(save_path)
                    logger.info(f"Saved state to {save_path}")

                # save the model
                pipeline.save_pretrained(args.output_dir)
                if args.push_to_hub:
                    repo.push_to_hub(commit_message=f"Epoch {epoch}", blocking=False)

    accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
