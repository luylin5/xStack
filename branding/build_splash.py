"""Compose the approved vector identity and exact splash copy."""
from pathlib import Path
from html import escape
import re
from PIL import Image, ImageDraw, ImageFont

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'branding'
W,H=1980,1350
TRIM=90  # Remove 30 display pixels of outer whitespace on each side.
EXPORT_W,EXPORT_H=W-2*TRIM,H-2*TRIM
BG='#061B43'
WHITE='#F2F7FC'
MUTED='#A6BAD2'
CYAN='#35CEF3'
VERSION='v1.5'
AUTHOR='Yu-Lin Lu'
YEAR='2026'
image=Image.new('RGB',(W,H),BG)
draw=ImageDraw.Draw(image)
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{EXPORT_W}" height="{EXPORT_H}" viewBox="{TRIM} {TRIM} {EXPORT_W} {EXPORT_H}"><rect width="{W}" height="{H}" fill="{BG}"/>']

def text(x,y,content,size,color=WHITE,bold=False):
    font=ImageFont.truetype('C:/Windows/Fonts/seguisb.ttf' if bold else 'C:/Windows/Fonts/segoeui.ttf',size)
    draw.text((x,y),content,font=font,fill=color,anchor='ls')
    svg.append(f'<text x="{x}" y="{y}" fill="{color}" font-family="Segoe UI,Arial,sans-serif" font-size="{size}" font-weight="{600 if bold else 400}">{escape(content)}</text>')

def right_text(right,y,content,size,color=MUTED):
    font=ImageFont.truetype('C:/Windows/Fonts/segoeui.ttf',size)
    text(right-font.getlength(content),y,content,size,color)

# Keep the selected mark intact; compose it at a fixed aspect ratio.
with Image.open(ROOT/'design-crystal-icon/xStack-crystal-soft.png') as source:
    image.paste(source.convert('RGB').resize((740,740),Image.Resampling.LANCZOS),(150,245))
mark=(ROOT/'design-crystal-icon/xStack-crystal-soft.svg').read_text(encoding='utf-8')
mark=re.sub(r'^<svg[^>]*>|</svg>$','',mark)
svg.append('<g transform="translate(150 245) scale(0.72265625)">'+mark+'</g>')

text(1040,548,'xStack',226,bold=True)
text(1050,674,'PXRD Viewer',78,CYAN)
text(1054,787,'View  /  Compare  /  Analyze',43,MUTED)
right_text(1800,164,VERSION,43)

draw.line((180,1100,1800,1100),fill='#25405F',width=2)
svg.append('<path d="M180 1100 H1800" stroke="#25405F" stroke-width="2"/>')
text(180,1186,'Developed by '+AUTHOR,43,MUTED)
right_text(1800,1186,'© '+YEAR,43)
svg.append('</svg>')
image=image.crop((TRIM,TRIM,W-TRIM,H-TRIM))
image.save(OUT/'starting_fig.png')
image.resize((EXPORT_W//3,EXPORT_H//3),Image.Resampling.LANCZOS).save(OUT/'splash-preview.png')
(OUT/'starting_fig.svg').write_text(''.join(svg),encoding='utf-8')
print(f'Created {EXPORT_W} x {EXPORT_H} splash and {EXPORT_W//3} x {EXPORT_H//3} preview.')
