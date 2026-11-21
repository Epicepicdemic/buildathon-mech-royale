import math, time
import mechfight as mf
from navigation import Grid

def lead(origin, target, velocity, speed):
    r=[target[i]-origin[i] for i in range(3)]; a=sum(v*v for v in velocity)-speed*speed; b=2*sum(r[i]*velocity[i] for i in range(3)); c=sum(x*x for x in r); disc=b*b-4*a*c
    roots=[]
    if abs(a)>1e-9 and disc>=0: roots=[v for v in ((-b-math.sqrt(disc))/(2*a),(-b+math.sqrt(disc))/(2*a)) if v>0]
    t=min(roots) if roots else math.sqrt(c)/speed
    p=[r[i]+velocity[i]*t for i in range(3)]; return [math.atan2(p[1],p[0]),math.atan2(p[2],math.hypot(p[0],p[1]))]
def angle(a,b): return abs((a-b+math.pi)%(2*math.pi)-math.pi)
class Bot:
    name='Wilce'
    def __init__(self,hello):
        self.cfg=hello['config']; self.grid=Grid(); self.path=[]; self.goal=None; self.last_plan=-100; self.last_pos=None; self.stuck=0; self.opening=True; self.hurt=-100; self.healing=False; self.attacker=None
    def act(self,o):
        now=time.perf_counter(); me=o['self']; pos=me['pos']; t=o['t']; g=self.grid; act={'tick':o['tick']}; enemies=o.get('mechs',[]); threats=[e for e in enemies if e['kind']=='mech']
        for event in o.get('events',[]):
            if event['type']=='damaged' and event.get('bearing') is not None: self.hurt=t; self.attacker=event['bearing']
        hp=me['hp']/max(1,me['max_hp']); self.healing = (hp<.48 or (self.healing and hp<.85)) and me['in_zone']
        if o.get('offer'):
            priorities={'plating':100,'armor':90,'gun_rate':75,'gun_damage':72,'regen':70,'speed':60}
            act['pick']=max(range(len(o['offer']['choices'])),key=lambda i:priorities.get(o['offer']['choices'][i]['id'],20))
        weapon_kinds=[w['kind'] for w in me['weapons'] if w]; reach=self.cfg['loot']['pickup_radius']
        if not self.opening:
            for p in o.get('pickups',[]):
                if p['kind'] not in weapon_kinds and p['kind'] in ('laser','lightning') and len(weapon_kinds)<2 and math.dist(pos[:2],p['pos'][:2])<=reach and abs(pos[2]-p['pos'][2])<=4: act['take']=p['id']; break
        zone=o['zone']; center=zone['next_center']; radius=zone['next_radius']; dist=math.dist(pos[:2],center); timeleft=max(.1,zone['shrink_end']-t); speed=me['stats'].get('boost_speed',19.5)
        # Aim for a margin inside the next circle; use its center if the circle is small.
        target_radius=max(0,radius-25); frac=max(0,dist-target_radius)/max(dist,1); goal=[pos[0]+(center[0]-pos[0])*frac,pos[1]+(center[1]-pos[1])*frac]
        urgent=not me['in_zone'] or (dist>target_radius and (dist-target_radius)/max(speed,1)+12>timeleft)
        # Cover from reported positions and approximate heard bearings. Sounds have noisy bearings.
        sources=[e['pos'] for e in threats]
        sources += [[pos[0]+math.cos(s['bearing'])*({'near':45,'mid':110,'far':200}.get(s['dist'],110)),pos[1]+math.sin(s['bearing'])*({'near':45,'mid':110,'far':200}.get(s['dist'],110))] for s in o.get('sounds',[]) if s['kind'] not in ('death','drop','crate')]
        if self.opening and not sources: self.opening=False
        if (self.opening or self.healing) and sources and not urgent:
            base=g.nearest(pos); bx=base%g.nx; by=base//g.nx; best=None
            for r in range(0,13):
                if time.perf_counter() > now + .004: break
                for dy in range(-r,r+1):
                    for dx in range(-r,r+1):
                        if max(abs(dx),abs(dy))!=r: continue
                        x=bx+dx;y=by+dy
                        if not (0<=x<g.nx and 0<=y<g.ny): continue
                        n=y*g.nx+x
                        if g.value(n,0): continue
                        p=g.xy(n)
                        if all(not g.visible(p,s) for s in sources):
                            score=math.dist(pos[:2],p)+g.value(n,3)/100
                            if best is None or score<best[0]: best=(score,p)
                if best: break
            if best: goal=list(best[1]); self.opening=math.dist(pos[:2],goal)>5 if self.opening else False
            elif self.opening and time.perf_counter() <= now + .004: self.opening=False
        if not self.opening and not self.healing and not urgent:
            choices=[c for c in o.get('cubes',[]) if abs(c['pos'][2]-pos[2])<4 and math.dist(c['pos'][:2],pos[:2])<50]
            drops=[d for d in o.get('drops',[]) if d['lands_in']<15 and math.dist(d['pos'][:2],pos[:2])<100 and not threats]
            if choices: goal=min(choices,key=lambda c:math.dist(c['pos'][:2],pos[:2]))['pos'][:2]
            elif drops: goal=min(drops,key=lambda d:math.dist(d['pos'][:2],pos[:2]))['pos'][:2]
            elif dist<=target_radius and not threats: goal=[20,-85] if math.dist([20,-85],center)<target_radius else goal
        if self.goal is None or math.dist(goal,self.goal)>12 or (t-self.last_plan>3 and not self.path):
            # Bound work on each decision; retry later if search cannot finish.
            path,_=g.route(pos,goal,0 if urgent else .45,now+.009)
            self.last_plan=t
            if path: self.path=path; self.goal=goal
        while len(self.path)>1 and math.dist(pos[:2],g.xy(self.path[1]))<4: self.path.pop(0)
        dest=g.xy(self.path[1]) if len(self.path)>1 else (goal if g.visible(pos,goal,0) else pos)
        dx=dest[0]-pos[0]; dy=dest[1]-pos[1]; d=math.hypot(dx,dy)
        move=[dx/d,dy/d] if d>2 else [0,0]
        if not urgent and not threats and not self.opening: move=[v*min(1,4.9/max(me['stats'].get('walk_speed',11.7),1)) for v in move]
        act['move']=move; act['boost']=urgent and me['energy']>10
        if self.last_pos is not None and d>4 and math.dist(pos[:2],self.last_pos)<.25: self.stuck+=1
        else: self.stuck=0
        if self.stuck>12: act['jump']=bool(me['grounded']); act['boost']=not me['grounded'] and me['energy']>10
        self.last_pos=pos[:2]
        if t-self.hurt<3 and self.attacker is not None: act['aim']=[self.attacker,0]
        else: act['aim']=[math.atan2(dy,dx) if d>2 else me['aim'][0],0]
        if not self.opening:
            candidates=[e for e in enemies if g.visible(pos,e['pos'],1) and math.dist(pos[:2],e['pos'][:2])<230]
            if candidates:
                e=min(candidates,key=lambda e:math.dist(pos[:2],e['pos'][:2])*.8+e.get('hp',1000)*.09+(100 if e['kind']=='npc' else 0)); distance=math.dist(pos[:2],e['pos'][:2]); finish=e.get('hp',1000)<250 and distance<100
                if not self.healing or finish:
                    gun=next((w for w in me['weapons'] if w and w['kind']=='gun'),None); speedshot=gun['stats'].get('speed',252) if gun else 252
                    origin=[pos[0],pos[1],pos[2]+me['stats'].get('muzzle_height',7)]; target=[e['pos'][0],e['pos'][1],e['pos'][2]+(4.5 if e['kind']=='mech' else 1.75)]
                    aim=lead(origin,target,e.get('vel',[0,0,0]),speedshot); act['aim']=aim; aligned=angle(me['aim'][0],aim[0])<.06 and abs(me['aim'][1]-aim[1])<.06
                    fire=[False,False]
                    for i,w in enumerate(me['weapons']):
                        if not w: continue
                        kind=w['kind']; stats=w['stats']
                        if kind=='gun': fire[i]=aligned and distance<stats.get('range',230)
                        elif kind=='laser': fire[i]=aligned and distance<stats.get('range',280) and w.get('charge',0)<stats.get('charge',.75)
                        elif kind=='lightning': fire[i]=aligned and distance<stats.get('range',45) and me['energy']>5
                    act['fire']=fire
        for w in me['weapons']:
            if w and w['kind']=='gun' and w.get('ammo',99)<4 and not any(act.get('fire',[])) and not w.get('reload'): act['reload']=True
        return act
if __name__=='__main__': mf.run(Bot)
