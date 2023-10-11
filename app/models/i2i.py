# -*- coding: utf-8 -*-
"""Img2imgGenerationStableDiffusion-Depth2img_PythonCodeTutorial

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1k9RnU0yCXBTCwyBvK9AtqKZywoRYgS_p

# Part 1

Note: Before running the code, make sure you upload [the images](https://github.com/x4nth055/pythoncode-tutorials/tree/master/machine-learning/depth2image-stable-diffusion) you want to edit to Colab.
"""

# Commented out IPython magic to ensure Python compatibility.
# %pip install --quiet --upgrade diffusers transformers scipy ftfy

# Commented out IPython magic to ensure Python compatibility.
# %pip install --quiet --upgrade accelerate

import numpy as np
from tqdm import tqdm
from PIL import Image

import torch
from torch import autocast

from transformers import CLIPTextModel, CLIPTokenizer
from transformers import DPTForDepthEstimation, DPTFeatureExtractor, DPTImageProcessor 

from diffusers import AutoencoderKL, UNet2DConditionModel
from diffusers.schedulers.scheduling_pndm import PNDMScheduler

"""## Model definition"""

class DiffusionPipeline:

    def __init__(self,
                 vae,
                 tokenizer,
                 text_encoder,
                 unet,
                 scheduler):

        self.vae = vae
        self.tokenizer = tokenizer
        self.text_encoder = text_encoder
        self.unet = unet
        self.scheduler = scheduler
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'


    def get_text_embeds(self, text):
        # tokenize the text
        text_input = self.tokenizer(text,
                                    padding='max_length',
                                    max_length=tokenizer.model_max_length,
                                    truncation=True,
                                    return_tensors='pt')
        # embed the text
        with torch.no_grad():
            text_embeds = self.text_encoder(text_input.input_ids.to(self.device))[0]
        return text_embeds


    def get_prompt_embeds(self, prompt):
        if isinstance(prompt, str):
            prompt = [prompt]
        # get conditional prompt embeddings
        cond_embeds = self.get_text_embeds(prompt)
        # get unconditional prompt embeddings
        uncond_embeds = self.get_text_embeds([''] * len(prompt))
        # concatenate the above 2 embeds
        prompt_embeds = torch.cat([uncond_embeds, cond_embeds])
        return prompt_embeds



    def decode_img_latents(self, img_latents):
        img_latents = 1 / self.vae.config.scaling_factor * img_latents
        with torch.no_grad():
            img = self.vae.decode(img_latents).sample

        img = (img / 2 + 0.5).clamp(0, 1)
        img = img.cpu().permute(0, 2, 3, 1).float().numpy()
        return img



    def transform_img(self, img):
        # scale images to the range [0, 255] and convert to int
        img = (img * 255).round().astype('uint8')
        # convert to PIL Image objects
        img = [Image.fromarray(i) for i in img]
        return img


    def encode_img_latents(self, img, latent_timestep):
        if not isinstance(img, list):
            img = [img]

        img = np.stack([np.array(i) for i in img], axis=0)
        # scale images to the range [-1, 1]
        img = 2 * ((img / 255.0) - 0.5)
        img = torch.from_numpy(img).float().permute(0, 3, 1, 2)
        img = img.to(self.device)

        # encode images
        img_latents_dist = self.vae.encode(img)
        img_latents = img_latents_dist.latent_dist.sample()

        # scale images
        img_latents = self.vae.config.scaling_factor * img_latents

        # add noise to the latents
        noise = torch.randn(img_latents.shape).to(self.device)
        img_latents = self.scheduler.add_noise(img_latents, noise, latent_timestep)

        return img_latents

class Depth2ImgPipeline(DiffusionPipeline):
    def __init__(self,
                 vae,
                 tokenizer,
                 text_encoder,
                 unet,
                 scheduler,
                 depth_feature_extractor,
                 depth_estimator):

        super().__init__(vae, tokenizer, text_encoder, unet, scheduler)

        self.depth_feature_extractor = depth_feature_extractor
        self.depth_estimator = depth_estimator


    def get_depth_mask(self, img):
        if not isinstance(img, list):
            img = [img]

        width, height = img[0].size

        # pre-process the input image and get its pixel values
        pixel_values = self.depth_feature_extractor(img, return_tensors="pt").pixel_values

        # use autocast for automatic mixed precision (AMP) inference
        with autocast('cpu'):
            depth_mask = self.depth_estimator(pixel_values).predicted_depth

        # get the depth mask
        depth_mask = torch.nn.functional.interpolate(depth_mask.unsqueeze(1),
                                                     size=(height//8, width//8),
                                                     mode='bicubic',
                                                     align_corners=False)

        # scale the mask to range [-1, 1]
        depth_min = torch.amin(depth_mask, dim=[1, 2, 3], keepdim=True)
        depth_max = torch.amax(depth_mask, dim=[1, 2, 3], keepdim=True)
        depth_mask = 2.0 * (depth_mask - depth_min) / (depth_max - depth_min) - 1.0
        depth_mask = depth_mask.to(self.device)

        # replicate the mask for classifier free guidance
        depth_mask = torch.cat([depth_mask] * 2)
        return depth_mask




    def denoise_latents(self,
                        img,
                        prompt_embeds,
                        depth_mask,
                        strength,
                        num_inference_steps=50,
                        guidance_scale=7.5,
                        height=512, width=512):

        # clip the value of strength to ensure strength lies in [0, 1]
        strength = max(min(strength, 1), 0)

        # compute timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        init_timestep = int(num_inference_steps * strength)
        t_start = num_inference_steps - init_timestep

        timesteps = self.scheduler.timesteps[t_start: ]
        num_inference_steps = num_inference_steps - t_start

        latent_timestep = timesteps[:1].repeat(1)

        latents = self.encode_img_latents(img, latent_timestep)

        # use autocast for automatic mixed precision (AMP) inference
        with autocast('cpu'):
            for i, t in tqdm(enumerate(timesteps)):
                latent_model_input = torch.cat([latents] * 2)
                latent_model_input = torch.cat([latent_model_input, depth_mask], dim=1)

                # predict noise residuals
                with torch.no_grad():
                    noise_pred = self.unet(latent_model_input, t, encoder_hidden_states=prompt_embeds)['sample']

                # separate predictions for unconditional and conditional outputs
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(2)

                # perform guidance
                noise_pred = noise_pred_uncond + guidance_scale * (noise_pred_text - noise_pred_uncond)

                # remove the noise from the current sample i.e. go from x_t to x_{t-1}
                latents = self.scheduler.step(noise_pred, t, latents)['prev_sample']

        return latents


    def __call__(self,
                 prompt,
                 img,
                 strength=0.8,
                 num_inference_steps=50,
                 guidance_scale=7.5,
                 height=512, width=512):


        prompt_embeds = self.get_prompt_embeds(prompt)

        depth_mask = self.get_depth_mask(img)

        latents = self.denoise_latents(img,
                                       prompt_embeds,
                                       depth_mask,
                                       strength,
                                       num_inference_steps,
                                       guidance_scale,
                                       height, width)

        depth2img = self.decode_img_latents(latents)

        depth2img = self.transform_img(img)
        print("123")
        return depth2img

"""## Create instance of the model"""

device = 'cpu'

# Load autoencoder
vae = AutoencoderKL.from_pretrained('stabilityai/stable-diffusion-2-depth', subfolder='vae').to(device)

# Load tokenizer and the text encoder
tokenizer = CLIPTokenizer.from_pretrained('stabilityai/stable-diffusion-2-depth', subfolder='tokenizer')
text_encoder = CLIPTextModel.from_pretrained('stabilityai/stable-diffusion-2-depth', subfolder='text_encoder').to(device)

# Load UNet model
unet = UNet2DConditionModel.from_pretrained('stabilityai/stable-diffusion-2-depth', subfolder='unet').to(device)

# Load scheduler
scheduler = PNDMScheduler(beta_start=0.00085,
                          beta_end=0.012,
                          beta_schedule='scaled_linear',
                          num_train_timesteps=1000)

# Load DPT Depth Estimator
depth_estimator = DPTForDepthEstimation.from_pretrained('stabilityai/stable-diffusion-2-depth', subfolder='depth_estimator')

# Load DPT Feature Extractor
depth_feature_extractor = DPTImageProcessor.from_pretrained('stabilityai/stable-diffusion-2-depth', subfolder='feature_extractor')

depth2img = Depth2ImgPipeline(vae,
                              tokenizer,
                              text_encoder,
                              unet,
                              scheduler,
                              depth_feature_extractor,
                              depth_estimator)

"""## Examples"""

# import urllib.parse as parse
# import os
# import requests

# # a function to determine whether a string is a URL or not
# def is_url(string):
#     try:
#         result = parse.urlparse(string)
#         return all([result.scheme, result.netloc, result.path])
#     except:
#         return False


# # a function to load an image
# def load_image(image_path):
#     if is_url(image_path):
#         return Image.open(requests.get(image_path, stream=True).raw)
#     elif os.path.exists(image_path):
#         return Image.open(image_path)


# url = "http://images.cocodataset.org/val2017/000000039769.jpg"
# img = load_image(url)
# img
img = r"E:\codified\gen_ai\House_image_genAI\app\assests\input_img\master1 (1).png"
im = Image.open(img)
# im.show()
print("45")

prompt = "colourfull plantation on windows"

depth2img(prompt, im)[0]




# img = load_image("image16.png")
# img

# prompt = "A boulder with gemstones falling down a hill"
# depth2img(prompt, img)[0]

# img = load_image("image11.png").resize((512, 512))
# img

# import gc
# import torch

# # Run this cell if you get OOM - Out of Memory - errors
# torch.cuda.empty_cache()
# gc.collect()
# torch.cuda.empty_cache()
# gc.collect()

# # just to check GPU memory
# !nvidia-smi

# prompt = "A futuristic city on the edge of space, a robotic bionic singularity portal, sci fi, utopian, tim hildebrandt, wayne barlowe, bruce pennington, donato giancola, larry elmore"
# depth2img(prompt, img)[0]

# """# Part 2"""

# import torch
# import requests
# from PIL import Image
# from diffusers import StableDiffusionDepth2ImgPipeline

# pipe = StableDiffusionDepth2ImgPipeline.from_pretrained(
#     "stabilityai/stable-diffusion-2-depth",
#     torch_dtype=torch.float16,
# ).to("cuda")

# """## Impact of negative prompt example"""

# img = load_image("https://images.pexels.com/photos/406152/pexels-photo-406152.jpeg?auto=compress&cs=tinysrgb&w=600")
# img

# prompt = "A salad with tomatoes and guanas chips mixed with ketchup and mustard and bay leaf and guacamole and onions and ketchup and luscious patty with sesame seeds and cashews and onions and ketchup, ethereal,"
# pipe(prompt=prompt, image=img, negative_prompt=None, strength=0.7).images[0]

# prompt = "A salad with tomatoes and guanas chips mixed with ketchup and mustard and bay leaf and guacamole and onions and ketchup and luscious patty with sesame seeds and cashews and onions and ketchup, ethereal,"
# n_prompt = "ugly, deformed, not detailed, bad architectures, blurred, too much blurred, motion blur"
# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.7).images[0]

# img = load_image("image15.png")
# img

# prompt = "Last remaining old man on earth"
# n_prompt = "bad anatomy, ugly, wrinkles"
# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.7).images[0]

# """## Changing strength - Futuristic city example"""

# img = load_image("image11.png")
# img

# prompt = "A futuristic city"
# pipe(prompt=prompt, image=img, negative_prompt=None, strength=0.7).images[0]

# prompt = "Futuristic city, modern, highly detailed, aesthetic, octane render, 8K, UHD, photoshopped"
# n_prompt = "ugly, deformed, not detailed, bad architectures, blurred, too much blurred, motion blur"
# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.7).images[0]

# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.1).images[0]

# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.5).images[0]

# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.9).images[0]

# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=1).images[0]

# """## Article beginning examples"""

# img = load_image("image12.png")
# img

# prompt = "World war, aesthetic"
# n_prompt = "bad looking, deformed, wholesome"
# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.9).images[0]

# img = load_image("image3.png")

# prompt = "Beautiful anime landscape"
# n_prompt = "bad, deformed, ugly"
# pipe(prompt=prompt, image=img, negative_prompt=n_prompt, strength=0.7).images[0]

