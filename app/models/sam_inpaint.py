import gradio as gr
import numpy as np
import torch
from diffusers import StableDiffusionInpaintPipeline
from PIL import Image, ImageDraw
from segment_anything import SamPredictor, sam_model_registry

device = "cuda:0"

selected_pixel = []

pipe = StableDiffusionInpaintPipeline.from_pretrained(
    "stabilityai/stable-diffusion-2-inpainting", 
    torch_dtype=torch.float16, 
)

pipe =  pipe.to(device)

with gr.Blocks() as demo: 
    with gr.Row():
        input_img = gr.Image(label="Input")
        mask_img = gr.Image(label="Mas")
        output_img = gr.Image(label="Output")

    with gr.Blocks():
            prompt_text = gr. Textbox(lines=1, label="Prompt")
            mask_points = gr.Textbox(lines =1, label="segmentation coords")
    with gr.Row():

        submit = gr.Button("Submit")

    
    def generate_binary_mask(mask_points):
        image_width = 950
        image_height = 550
        # Create a black and white image with a black background
        shape = Image.new('L', (image_width, image_height), 0)

        # Create a white mask in the image
        draw = ImageDraw.Draw(shape)
        draw.polygon(mask_points, outline=255, fill=255)

        # Convert the grayscale image to a binary mask
        mask = np.array(shape)

        return mask
        
    def inpaint (image, mask, prompt):
        image = Image.fromarray(image)
        mask = Image.fromarray(mask)
        image = image.resize((512, 512))
        mask = mask.resize((512, 512))
        output = pipe(
            prompt=prompt, 
            image=image, 
            mask_image=mask
            ).images[0]
        
        return output
    
    input_img.select(generate_binary_mask, [input_img], [mask_points], [mask_img])
    
    submit.click(
        inpaint, 
        inputs = [input_img, mask_img, prompt_text, mask_points],
        outputs=[output_img],
    )
    
    
if __name__ == "__main__":
    demo.launch()

