from pathlib import Path
import json
import numpy as np
from PIL import Image, ImageDraw

ROOT=Path(__file__).resolve().parent
NAME='xStack-crystal-soft'
BG='#061B43'
FG='#35CEF3'
A,B,C,D,E,F=(512,100),(840,292),(840,732),(512,924),(184,732),(184,292)
G,H,I,J=(332,376),(692,376),(692,648),(332,648)

def offset(poly, amount):
    p=np.array(poly,dtype=float)
    if sum(p[i,0]*p[(i+1)%len(p),1]-p[i,1]*p[(i+1)%len(p),0] for i in range(len(p)))<0:
        p=p[::-1]
    v=np.roll(p,-1,axis=0)-p
    n=np.stack([-v[:,1],v[:,0]],axis=1)/np.linalg.norm(v,axis=1)[:,None]
    q=p+amount*n
    out=[]
    for i in range(len(p)):
        prev=(i-1)%len(p)
        t=np.linalg.solve(np.column_stack([v[prev],-v[i]]),q[i]-q[prev])[0]
        out.append(q[prev]+t*v[prev])
    return np.array(out)

def rounded(poly, radius):
    p=np.array(poly,dtype=float)
    before=[];after=[]
    for i,b in enumerate(p):
        u=p[i-1]-b;v=p[(i+1)%len(p)]-b
        distance=min(radius,np.linalg.norm(u)*.28,np.linalg.norm(v)*.28)
        before.append(b+u/np.linalg.norm(u)*distance)
        after.append(b+v/np.linalg.norm(v)*distance)
    fmt=lambda v:f'{v[0]:.6f},{v[1]:.6f}'
    d='M '+fmt(before[0]); samples=[]
    for i,b in enumerate(p):
        d+=' L '+fmt(before[i])+' Q '+fmt(b)+' '+fmt(after[i])
        for t in np.linspace(0,1,17):
            samples.append((1-t)**2*before[i]+2*(1-t)*t*b+t*t*after[i])
    return d+' Z',np.array(samples)

# Fifty-pixel structural edges (previous version: 36); round the negative spaces too.
shapes=[(*rounded(offset([A,B,C,D,E,F],-25),34),FG)]
for face in [[A,F,G],[A,B,H],[D,E,J],[D,C,I],[F,E,J,G],[B,C,I,H],[A,G,J,D],[A,H,I,D]]:
    shapes.append((*rounded(offset(face,25),22),BG))
svg=f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024"><rect width="1024" height="1024" fill="{BG}"/>'
for d,poly,color in shapes:
    svg+=f'<path d="{d}" fill="{color}"/>'
svg+='</svg>'
(ROOT/(NAME+'.svg')).write_text(svg,encoding='utf-8')

def render(size):
    scale=4
    im=Image.new('RGB',(size*scale,size*scale),BG)
    draw=ImageDraw.Draw(im)
    for d,poly,color in shapes:
        # Native vector rasterization uses paired coverage to prevent rounding drift.
        points=[tuple(point*size*scale/1024-.5) for point in poly]
        draw.polygon(points,fill=color)
    arr=np.array(im,dtype=np.uint16)
    arr=(arr+arr[::-1,::-1]+1)//2
    arr=arr.reshape(size,scale,size,scale,3).sum(axis=(1,3))
    arr=((arr+scale*scale//2)//(scale*scale)).astype(np.uint8)
    assert np.array_equal(arr,arr[::-1,::-1])
    return Image.fromarray(arr)

render(1024).save(ROOT/(NAME+'.png'))
sizes=[16,24,32,48,64,128,256]
frames=[render(s) for s in sizes]
frames[-1].save(ROOT/(NAME+'.ico'),sizes=[(s,s) for s in sizes],append_images=frames[:-1])
checks={}
with Image.open(ROOT/(NAME+'.ico')) as ico:
    for size in ico.ico.sizes():
        p=np.array(ico.ico.getimage(size))
        diff=int(np.any(p!=p[::-1,::-1],axis=2).sum())
        assert diff==0
        checks[str(size)]=diff
report={'stroke_width':50,'previous_stroke_width':36,'png_180_degree_difference_pixels':0,'ico_180_degree_difference_pixels':checks}
(ROOT/'soft-check.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report))

# Native rounded-square background; the crystal itself stays identical.
ROUND_NAME='xStack-crystal-rounded'
RADIUS=200
round_svg=svg.replace(f'<rect width="1024" height="1024" fill="{BG}"/>',
                      f'<rect width="1024" height="1024" rx="{RADIUS}" fill="{BG}"/>')
(ROOT/(ROUND_NAME+'.svg')).write_text(round_svg,encoding='utf-8')

def render_rounded(size):
    im=render(size).convert('RGBA')
    scale=4
    coord=(np.arange(size*scale)+.5)*1024/(size*scale)
    q=np.maximum(np.abs(coord-512)-(512-RADIUS),0)
    inside=(q[:,None]**2+q[None,:]**2 <= RADIUS**2)
    count=inside.reshape(size,scale,size,scale).sum(axis=(1,3))
    alpha=((count*255+8)//16).astype(np.uint8)
    im.putalpha(Image.fromarray(alpha))
    p=np.array(im)
    assert np.array_equal(p,p[::-1,::-1])
    assert alpha[0,0]==0 and alpha[size//2,size//2]==255
    return im

render_rounded(1024).save(ROOT/(ROUND_NAME+'.png'))
rounded_frames=[render_rounded(s) for s in sizes]
rounded_frames[-1].save(ROOT/(ROUND_NAME+'.ico'),sizes=[(s,s) for s in sizes],append_images=rounded_frames[:-1])
with Image.open(ROOT/(ROUND_NAME+'.ico')) as ico:
    assert ico.ico.sizes()=={(s,s) for s in sizes}
    for size in ico.ico.sizes():
        p=np.array(ico.ico.getimage(size).convert('RGBA'))
        assert p[0,0,3]==0
        assert np.array_equal(p,p[::-1,::-1])
print('Rounded PNG and all ICO sizes: transparent corners and exact center symmetry verified.')
