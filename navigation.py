import heapq, math, struct, time, zlib

class Grid:
    def __init__(self, path='map.dat'):
        with open(path,'rb') as f:
            self.nx,self.ny,self.x0,self.y0,self.res=struct.unpack('<IIddd',f.read(32)); self.data=zlib.decompress(f.read())
    def cell(self,p):
        i=max(0,min(self.nx-1,int((p[0]-self.x0)/self.res))); j=max(0,min(self.ny-1,int((p[1]-self.y0)/self.res))); return j*self.nx+i
    def xy(self,n): return (self.x0+(n%self.nx+.5)*self.res,self.y0+(n//self.nx+.5)*self.res)
    def value(self,n,k): return self.data[n*5+k]
    def nearest(self,p):
        n=self.cell(p)
        if not self.value(n,0): return n
        x=n%self.nx; y=n//self.nx
        for r in range(1,30):
            choices=[]
            for j in range(max(0,y-r),min(self.ny,y+r+1)):
                for i in range(max(0,x-r),min(self.nx,x+r+1)):
                    q=j*self.nx+i
                    if not self.value(q,0): choices.append(((i-x)**2+(j-y)**2,q))
            if choices: return min(choices)[1]
        return n
    def visible(self,a,b,layer=2):
        dx=b[0]-a[0]; dy=b[1]-a[1]; steps=max(1,int(math.hypot(dx,dy)/2))
        return all(not self.value(self.cell((a[0]+dx*i/steps,a[1]+dy*i/steps)),layer) for i in range(1,steps))
    def neighbors(self,n):
        x=n%self.nx; y=n//self.nx
        for dy in (-1,0,1):
            for dx in (-1,0,1):
                if not (dx or dy) or not (0<=x+dx<self.nx and 0<=y+dy<self.ny): continue
                q=n+dy*self.nx+dx
                if self.value(q,0): continue
                if dx and dy and (self.value(n+dx,0) or self.value(n+dy*self.nx,0)): continue
                yield q, 1.41421356237 if dx and dy else 1.
    def route(self,start,goal,weight=0,deadline=None):
        start=self.nearest(start); goal=self.nearest(goal); gx=goal%self.nx; gy=goal//self.nx
        def h(n): return math.hypot(n%self.nx-gx,n//self.nx-gy)
        heap=[(h(start),0,start)]; cost={start:0}; prev={}; count=0
        while heap:
            _,c,n=heapq.heappop(heap)
            if c!=cost.get(n): continue
            if n==goal:
                path=[n]
                while n!=start: n=prev[n]; path.append(n)
                return list(reversed(path)),c*self.res
            count+=1
            if deadline and count%64==0 and time.perf_counter()>deadline: return None,None
            for q,d in self.neighbors(n):
                penalty=1+weight*(self.value(q,3)/255 + (1-self.value(q,4)/255)*.3)
                nc=c+d*penalty
                if nc<cost.get(q,float('inf')):
                    cost[q]=nc; prev[q]=n; heapq.heappush(heap,(nc+h(q),nc,q))
        return None,None

class Search:
    """Resumable A* so long routes do not monopolize a decision."""
    def __init__(self,g,start,goal,weight):
        self.g=g; self.start=g.nearest(start); self.goal=g.nearest(goal); self.weight=weight
        self.gx=self.goal%g.nx; self.gy=self.goal//g.nx
        self.heap=[(self.h(self.start),0,self.start)]; self.cost={self.start:0}; self.prev={}
    def h(self,n): return math.hypot(n%self.g.nx-self.gx,n//self.g.nx-self.gy)
    def step(self,deadline):
        g=self.g; count=0
        while self.heap:
            _,c,n=heapq.heappop(self.heap)
            if c!=self.cost.get(n): continue
            if n==self.goal:
                path=[n]
                while n!=self.start: n=self.prev[n]; path.append(n)
                return list(reversed(path))
            for q,d in g.neighbors(n):
                nc=c+d*(1+self.weight*(g.value(q,3)/255+(1-g.value(q,4)/255)*.3))
                if nc<self.cost.get(q,float('inf')):
                    self.cost[q]=nc; self.prev[q]=n; heapq.heappush(self.heap,(nc+self.h(q),nc,q))
            count+=1
            if count%32==0 and time.perf_counter()>deadline: return None
        return []
