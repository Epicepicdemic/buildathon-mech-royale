"""Rasterize arena geometry and visibility at build time. Requires numpy."""
import json, math, struct, zlib
import numpy as np
w=json.load(open('arena/v2/world.json')); t=w['terrain']; R=4.; x0=t['minX']; y0=t['minY']; nx=int((t['nx']-1)/R); ny=int((t['ny']-1)/R)
x=x0+(np.arange(nx)+.5)*R; y=y0+(np.arange(ny)+.5)*R; X,Y=np.meshgrid(x,y)
terrain=np.fromfile('arena/v2/'+t['file'],dtype='<i2').reshape(t['ny'],t['nx'])*t['scale']; ground=terrain[np.clip((Y-y0).astype(int),0,t['ny']-1),np.clip((X-x0).astype(int),0,t['nx']-1)]
height=np.zeros((ny,nx))
def inside(r,X,Y):
    xs=np.array(r[::2]); ys=np.array(r[1::2]); out=np.zeros(X.shape,bool); j=len(xs)-1
    for i in range(len(xs)):
        if ys[j]!=ys[i]: out ^= ((ys[i]>Y)!=(ys[j]>Y)) & (X < (xs[j]-xs[i])*(Y-ys[i])/(ys[j]-ys[i])+xs[i])
        j=i
    return out
for p in w['prisms']:
    r=p['ring']; i0=max(0,int((min(r[::2])-x0)/R)-1); i1=min(nx,int((max(r[::2])-x0)/R)+2); j0=max(0,int((min(r[1::2])-y0)/R)-1); j1=min(ny,int((max(r[1::2])-y0)/R)+2)
    if i0>=i1 or j0>=j1: continue
    xx=X[j0:j1,i0:i1]; yy=Y[j0:j1,i0:i1]; g=ground[j0:j1,i0:i1]; m=inside(r,xx,yy)
    for hole in p.get('holes',[]):
        if hole: m &= ~inside(hole,xx,yy)
    m &= p['base']-g < 9
    h=height[j0:j1,i0:i1]; np.maximum(h,np.where(m,p['top']-g,0),out=h)
solid=height>1.2; sight=height>=9
# Conservative one-cell clearance for a 2.5m-radius body.
blocked=solid.copy()
for dy in (-1,0,1):
 for dx in (-1,0,1):
    if dx or dy: blocked |= np.roll(np.roll(solid,dy,0),dx,1)
blocked[[0,-1],:]=True; blocked[:,[0,-1]]=True
# Exposure is visible area fraction in a 200m disc; near-wall fraction encodes cover.
vis=np.full((ny,nx,32),200.,dtype=np.float32)
for a in range(32):
    angle=2*math.pi*a/32; live=np.ones((ny,nx),bool)
    for d in range(4,201,4):
        ii=np.rint(np.arange(nx)[None,:]+math.cos(angle)*d/R).astype(int); jj=np.rint(np.arange(ny)[:,None]+math.sin(angle)*d/R).astype(int)
        oob=(ii<0)|(ii>=nx)|(jj<0)|(jj>=ny); hit=sight[np.clip(jj,0,ny-1),np.clip(ii,0,nx-1)]|oob; stop=live&hit; vis[:,:,a][stop]=d; live &= ~stop
expo=np.mean((vis/200)**2,axis=2); near=np.mean(vis<=16,axis=2)
# One byte each: blocked, solid, sight, exposure and nearby wall.
arr=np.stack([blocked,solid,sight,np.round(expo*255),np.round(near*255)],axis=2).astype('uint8')
with open('map.dat','wb') as f: f.write(struct.pack('<IIddd',nx,ny,x0,y0,R)); f.write(zlib.compress(arr.tobytes(),9))
