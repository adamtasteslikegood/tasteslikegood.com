from PIL import Image, ImageDraw, ImageFont

# Create full-text logos that always fit the full domain
def create_full_text_logo(path, size, text, bg_color, text_color):
    img = Image.new("RGBA", size, bg_color)
    draw = ImageDraw.Draw(img)

    # Dynamic font sizing to ensure full text fits
    font_size = size[1] * 0.25
    font = None
    while font_size > 10:
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", int(font_size))
        except:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= size[0] * 0.92:
            break
        font_size -= 2

    x = (size[0] - (bbox[2] - bbox[0])) // 2
    y = (size[1] - (bbox[3] - bbox[1])) // 2
    draw.text((x, y), text, fill=text_color, font=font)

    img.save(path)

# Create assets
create_full_text_logo(
    "/mnt/data/tlg-fulltext-dark.png",
    (1600, 500),
    "tasteslikegood.com",
    (18, 18, 18, 255),
    (245, 245, 245, 255)
)

create_full_text_logo(
    "/mnt/data/tlg-fulltext-square.png",
    (1024, 1024),
    "tasteslikegood.com",
    (18, 18, 18, 255),
    (245, 245, 245, 255)
)

create_full_text_logo(
    "/mnt/data/tlg-fulltext-transparent.png",
    (1600, 500),
    "tasteslikegood.com",
    (0, 0, 0, 0),
    (18, 18, 18, 255)
)

"/mnt/data full-text logos created"