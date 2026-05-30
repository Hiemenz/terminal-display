"""
Thin CLI wrapper around display_eink.

Standalone:
    python src/display.py --image output/terminal.bmp [--config PATH]
"""
import os
import sys
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image
from display_eink import display_image
from config_loader import load_config, add_config_arg


def send_to_display(image_path):
    """Load image from path and push to e-ink."""
    image = Image.open(image_path)
    display_image(image, output_filename=image_path)


def main():
    parser = argparse.ArgumentParser(description='Send an image to the e-ink display')
    parser.add_argument('--image', required=True, help='Path to image file')
    add_config_arg(parser)
    args = parser.parse_args()

    if not os.path.isfile(args.image):
        print(f"Error: image not found: {args.image}")
        sys.exit(1)

    send_to_display(args.image)
    print("Display updated")


if __name__ == '__main__':
    main()
