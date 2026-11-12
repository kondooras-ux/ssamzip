"""쌤ZIP 아이콘(ssamzip.ico) 생성기. 아이콘을 바꾸고 싶으면 이 파일을 고쳐서 다시 실행하세요.
필요: pip install pillow"""
from PIL import Image, ImageDraw

S = 1024  # 크게 그린 뒤 줄여서 부드럽게


def gradient(size, top, bottom):
    img = Image.new("RGB", (size, size))
    px = img.load()
    for y in range(size):
        t = y / (size - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(size):
            px[x, y] = c
    return img


def build():
    bg = gradient(S, (64, 150, 255), (20, 80, 190))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((40, 40, S - 40, S - 40), radius=210, fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(bg, (0, 0), mask)

    d = ImageDraw.Draw(icon)
    cx, tooth_w, tooth_h, gap = S // 2, 120, 62, 26
    y, left = 150, True
    while y < 640:                                  # 지퍼 이빨: 좌우 번갈아
        x0 = cx - tooth_w if left else cx
        d.rounded_rectangle((x0, y, x0 + tooth_w, y + tooth_h), radius=16, fill=(255, 255, 255, 240))
        left = not left
        y += tooth_h // 2 + gap
    # 슬라이더(손잡이)
    d.rounded_rectangle((cx - 120, 600, cx + 120, 760), radius=48, fill=(255, 255, 255, 255))
    d.rounded_rectangle((cx - 52, 640, cx + 52, 720), radius=26, fill=(26, 96, 205, 255))
    d.rounded_rectangle((cx - 40, 760, cx + 40, 880), radius=30, fill=(255, 255, 255, 255))
    d.ellipse((cx - 22, 800, cx + 22, 844), fill=(26, 96, 205, 255))
    return icon


if __name__ == "__main__":
    big = build()
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.resize((256, 256), Image.LANCZOS).save("ssamzip.ico", sizes=sizes)
    big.resize((256, 256), Image.LANCZOS).save("ssamzip_preview.png")
    print("ssamzip.ico 생성 완료")
