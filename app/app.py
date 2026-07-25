import time
import threading
import os
import socket
from functools import wraps
from flask import Flask, request, jsonify
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from metrics import (
    REQUEST_COUNT, REQUEST_LATENCY, ACTIVE_REQUESTS,
    ERROR_COUNT, TASKS_CREATED, TASKS_DELETED, TASKS_IN_STORE
)

app = Flask(__name__)

task_store = {}
task_lock = threading.Lock()
task_counter = [0]

_health_ok = True
_ready_ok = True
_stress_thread = None


def track_metrics(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        method = request.method
        endpoint = request.path
        ACTIVE_REQUESTS.inc()
        start = time.time()
        try:
            response = f(*args, **kwargs)
            status = response[1] if isinstance(response, tuple) else 200
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status=str(status)).inc()
            return response
        except Exception as e:
            ERROR_COUNT.labels(method=method, endpoint=endpoint, error_type=type(e).__name__).inc()
            REQUEST_COUNT.labels(method=method, endpoint=endpoint, status='500').inc()
            raise
        finally:
            REQUEST_LATENCY.labels(method=method, endpoint=endpoint).observe(time.time() - start)
            ACTIVE_REQUESTS.dec()
    return decorated


# ── Task CRUD ──────────────────────────────────────────────────────────────

@app.route('/tasks', methods=['POST'])
@track_metrics
def create_task():
    data = request.get_json(silent=True) or {}
    title = data.get('title', '').strip()
    if not title:
        return jsonify({'error': 'title is required'}), 400
    with task_lock:
        task_counter[0] += 1
        tid = task_counter[0]
        task_store[tid] = {
            'id': tid,
            'title': title,
            'description': (data.get('description') or '').strip(),
            'priority': data.get('priority') if data.get('priority') in ('low', 'medium', 'high') else 'medium',
            'due_date': (data.get('due_date') or '').strip(),
            'category': (data.get('category') or '').strip(),
            'done': False,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        }
        TASKS_CREATED.inc()
        TASKS_IN_STORE.set(len(task_store))
    return jsonify(task_store[tid]), 201


@app.route('/tasks', methods=['GET'])
@track_metrics
def list_tasks():
    with task_lock:
        tasks = list(task_store.values())
    return jsonify(tasks), 200


@app.route('/tasks/<int:tid>', methods=['GET'])
@track_metrics
def get_task(tid):
    with task_lock:
        task = task_store.get(tid)
    if not task:
        return jsonify({'error': 'not found'}), 404
    return jsonify(task), 200


@app.route('/tasks/<int:tid>', methods=['PUT'])
@track_metrics
def update_task(tid):
    with task_lock:
        task = task_store.get(tid)
        if not task:
            return jsonify({'error': 'not found'}), 404
        data = request.get_json(silent=True) or {}
        if 'title' in data:
            task['title'] = data['title']
        if 'description' in data:
            task['description'] = data['description']
        if 'priority' in data and data['priority'] in ('low', 'medium', 'high'):
            task['priority'] = data['priority']
        if 'due_date' in data:
            task['due_date'] = data['due_date']
        if 'category' in data:
            task['category'] = data['category']
        if 'done' in data:
            task['done'] = bool(data['done'])
    return jsonify(task), 200


@app.route('/tasks/<int:tid>', methods=['DELETE'])
@track_metrics
def delete_task(tid):
    with task_lock:
        task = task_store.pop(tid, None)
        if task is None:
            return jsonify({'error': 'not found'}), 404
        TASKS_DELETED.inc()
        TASKS_IN_STORE.set(len(task_store))
    return jsonify({'deleted': tid}), 200


# ── Health & Readiness ─────────────────────────────────────────────────────

@app.route('/health')
def health():
    if _health_ok:
        return jsonify({'status': 'healthy'}), 200
    return jsonify({'status': 'unhealthy'}), 503


@app.route('/ready')
def ready():
    if _ready_ok:
        return jsonify({'status': 'ready'}), 200
    return jsonify({'status': 'not ready'}), 503


# ── Prometheus metrics ─────────────────────────────────────────────────────

@app.route('/metrics')
def metrics():
    return generate_latest(), 200, {'Content-Type': CONTENT_TYPE_LATEST}


# ── Stress endpoints ───────────────────────────────────────────────────────

def _burn_cpu(duration=120):
    end = time.time() + duration
    while time.time() < end:
        _ = [i * i for i in range(10000)]


def _fill_memory(mb=180):
    global _mem_block
    _mem_block = bytearray(mb * 1024 * 1024)
    time.sleep(120)
    del _mem_block


@app.route('/stress/cpu')
def stress_cpu():
    t = threading.Thread(target=_burn_cpu, args=(120,), daemon=True)
    t.start()
    return jsonify({'status': 'CPU stress started for 120s'}), 200


@app.route('/stress/memory')
def stress_memory():
    t = threading.Thread(target=_fill_memory, args=(180,), daemon=True)
    t.start()
    return jsonify({'status': 'Memory stress started — 180 MB allocated for 120s'}), 200


@app.route('/stress/crash')
def stress_crash():
    global _health_ok
    _health_ok = False
    return jsonify({'status': 'Health check will now return 503 — pod will appear crashed'}), 200


@app.route('/stress/not-ready')
def stress_not_ready():
    global _ready_ok
    _ready_ok = False
    return jsonify({'status': 'Readiness check will now return 503 — pod removed from service'}), 200


@app.route('/stress/reset')
def stress_reset():
    global _health_ok, _ready_ok
    _health_ok = True
    _ready_ok = True
    return jsonify({'status': 'All stress cleared — pod is healthy and ready'}), 200


_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>My Task Flow</title>
<style>
 :root{--bg:#eef1fb;--card:#fff;--pri:#4f46e5;--pri2:#7c3aed;--text:#0f172a;--muted:#94a3b8;--line:#eef0f4;}
 *{box-sizing:border-box;} html,body{margin:0;}
 body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,sans-serif;background:radial-gradient(1200px 500px at 50% -10%,#e6e9fb,transparent),var(--bg);color:var(--text);min-height:100vh;}
 .top{background:linear-gradient(135deg,var(--pri),var(--pri2));color:#fff;padding:1.7rem 1rem 3.4rem;box-shadow:0 8px 24px rgba(79,70,229,.25);}
 .wrap{max-width:680px;margin:0 auto;padding:0 1rem;}
 .brow{display:flex;justify-content:space-between;align-items:flex-start;}
 .brand{font-size:1.8rem;font-weight:800;letter-spacing:-.6px;display:flex;align-items:center;gap:.5rem;}
 .logo{width:30px;height:30px;border-radius:9px;background:rgba(255,255,255,.2);display:inline-flex;align-items:center;justify-content:center;font-size:1rem;}
 .tagline{color:#e0e7ff;font-size:.9rem;margin-top:.25rem;}
 .stat{text-align:right;} .stat .big{font-size:1.9rem;font-weight:800;line-height:1;} .stat .sm{font-size:.72rem;color:#e0e7ff;}
 .pbar{height:9px;background:rgba(255,255,255,.25);border-radius:99px;margin-top:1.1rem;overflow:hidden;}
 .pbar .fill{height:100%;background:#fff;width:0;border-radius:99px;transition:width .45s cubic-bezier(.4,0,.2,1);}
 .plabel{color:#e0e7ff;font-size:.75rem;margin-top:.4rem;display:flex;gap:.9rem;}
 .panel{background:var(--card);border-radius:20px;box-shadow:0 14px 34px rgba(2,6,23,.12);margin-top:-2.4rem;overflow:hidden;}
 form.add{padding:1rem;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:.55rem;}
 .add-main{display:flex;gap:.5rem;}
 .add-main input{flex:1;border:1px solid var(--line);border-radius:12px;padding:.85rem 1rem;font-size:1rem;outline:none;background:#fafbff;}
 .add-main input:focus{border-color:var(--pri);background:#fff;box-shadow:0 0 0 3px rgba(79,70,229,.12);}
 .addbtn{background:var(--pri);color:#fff;border:0;border-radius:12px;padding:0 1.3rem;font-weight:700;cursor:pointer;font-size:.95rem;white-space:nowrap;transition:.15s;}
 .addbtn:hover{background:#4338ca;transform:translateY(-1px);}
 .add-meta{display:flex;gap:.5rem;flex-wrap:wrap;}
 .add-meta select,.add-meta input{border:1px solid var(--line);border-radius:10px;padding:.55rem .65rem;font-size:.85rem;background:#fafbff;outline:none;color:var(--text);}
 .add-meta .desc{flex:1;min-width:160px;}
 .filters{padding:.75rem 1rem;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:.6rem;}
 .frow{display:flex;align-items:center;gap:.45rem;flex-wrap:wrap;}
 .flabel{font-size:.68rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;width:62px;flex:0 0 auto;}
 .tabs{display:flex;gap:.25rem;background:#f1f5f9;border-radius:10px;padding:.25rem;}
 .tab{border:0;background:none;padding:.4rem .8rem;border-radius:8px;font-size:.82rem;font-weight:600;color:var(--muted);cursor:pointer;}
 .tab.active{background:#fff;color:var(--pri);box-shadow:0 1px 3px rgba(2,6,23,.1);}
 .fchip{border:1px solid var(--line);background:#fff;color:#64748b;border-radius:99px;padding:.3rem .75rem;font-size:.75rem;font-weight:600;cursor:pointer;transition:.12s;}
 .fchip:hover{border-color:var(--pri);}
 .fchip.on{color:#fff;border-color:transparent;}
 .fchip.on.pf-high{background:#ef4444;} .fchip.on.pf-medium{background:#f59e0b;} .fchip.on.pf-low{background:#10b981;}
 .spacer{flex:1;}
 .search{border:1px solid var(--line);border-radius:10px;padding:.5rem .75rem;font-size:.85rem;outline:none;background:#fafbff;min-width:130px;}
 .sortsel{border:1px solid var(--line);border-radius:10px;padding:.45rem .5rem;font-size:.8rem;background:#fafbff;color:var(--text);outline:none;}
 .clearf{background:none;border:0;color:var(--pri);font-size:.75rem;font-weight:600;cursor:pointer;}
 ul{list-style:none;margin:0;padding:.25rem 0;}
 li{display:flex;align-items:flex-start;gap:.8rem;padding:.9rem 1rem;border-bottom:1px solid var(--line);animation:fade .25s ease;transition:background .15s;}
 li:hover{background:#fafbff;} li:last-child{border-bottom:0;}
 @keyframes fade{from{opacity:0;transform:translateY(4px);}to{opacity:1;transform:none;}}
 .cb{width:23px;height:23px;border:2px solid #cbd5e1;border-radius:7px;cursor:pointer;flex:0 0 auto;margin-top:.1rem;display:flex;align-items:center;justify-content:center;color:#fff;font-size:.8rem;transition:.15s;}
 .cb.on{background:var(--pri);border-color:var(--pri);}
 .body{flex:1;min-width:0;}
 .title{font-size:1rem;font-weight:600;word-break:break-word;} .title.s{text-decoration:line-through;color:var(--muted);}
 .desc{font-size:.85rem;color:#64748b;margin-top:.15rem;word-break:break-word;}
 .badges{display:flex;gap:.4rem;flex-wrap:wrap;margin-top:.5rem;}
 .pill{font-size:.68rem;font-weight:700;padding:.22rem .55rem;border-radius:99px;text-transform:capitalize;display:inline-flex;align-items:center;gap:.3rem;}
 .p-high{background:#fee2e2;color:#b91c1c;} .p-medium{background:#fef3c7;color:#b45309;} .p-low{background:#d1fae5;color:#047857;}
 .due{background:#f1f5f9;color:#475569;} .due.over{background:#fee2e2;color:#b91c1c;} .due.today{background:#e0e7ff;color:#4338ca;}
 .actions{display:flex;gap:.15rem;flex:0 0 auto;}
 .ico{border:0;background:none;cursor:pointer;font-size:1rem;padding:.32rem .42rem;border-radius:8px;color:var(--muted);}
 .ico:hover{background:#eef2ff;color:var(--pri);} .ico.del:hover{background:#fef2f2;color:#ef4444;}
 .empty{padding:2.6rem 1rem;text-align:center;color:var(--muted);}
 .foot{max-width:680px;margin:1rem auto 2.4rem;padding:0 1rem;color:#a3aed0;font-size:.75rem;display:flex;justify-content:space-between;align-items:center;}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e;margin-right:.4rem;vertical-align:middle;}
 .clearc{background:none;border:0;color:#a3aed0;font-size:.75rem;cursor:pointer;text-decoration:underline;}
 .clearc:hover{color:#ef4444;}
 .modal{position:fixed;inset:0;background:rgba(15,23,42,.55);display:none;align-items:center;justify-content:center;padding:1rem;z-index:9;backdrop-filter:blur(2px);}
 .modal.show{display:flex;}
 .mcard{background:#fff;border-radius:18px;padding:1.5rem;width:100%;max-width:440px;box-shadow:0 24px 60px rgba(2,6,23,.35);animation:fade .2s ease;}
 .mcard h3{margin:0 0 1rem;} .mcard label{font-size:.7rem;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.5px;}
 .mcard input,.mcard textarea,.mcard select{width:100%;border:1px solid var(--line);border-radius:10px;padding:.6rem .7rem;font-size:.9rem;margin:.25rem 0 .8rem;outline:none;font-family:inherit;background:#fafbff;}
 .mrow{display:flex;gap:.6rem;} .mrow>div{flex:1;}
 .mact{display:flex;justify-content:flex-end;gap:.5rem;margin-top:.4rem;}
 .mact button{border:0;border-radius:11px;padding:.65rem 1.15rem;font-weight:700;cursor:pointer;font-size:.9rem;}
 .cancel{background:#f1f5f9;color:#475569;} .save{background:var(--pri);color:#fff;}
</style>
</head>
<body>
 <div class="top"><div class="wrap">
   <div class="brow">
     <div><div class="brand"><span class="logo">&#10003;</span>My Task Flow</div><div class="tagline">Plan it. Track it. Done.</div></div>
     <div class="stat"><div class="big" id="pctBig">0%</div><div class="sm">completed</div></div>
   </div>
   <div class="pbar"><div class="fill" id="fill"></div></div>
   <div class="plabel"><span id="plabel">No tasks yet</span></div>
 </div></div>
 <div class="wrap">
  <div class="panel">
   <form class="add" onsubmit="return addTask(event)">
     <div class="add-main">
       <input id="title" placeholder="What needs to be done?" autocomplete="off">
       <button type="submit" class="addbtn">Add Task</button>
     </div>
     <div class="add-meta">
       <select id="priority" title="Priority"><option value="low">Low</option><option value="medium" selected>Medium</option><option value="high">High</option></select>
       <select id="category" title="Category"></select>
       <input id="due" type="date" title="Due date">
       <input id="desc" class="desc" placeholder="Description (optional)">
     </div>
   </form>

   <div class="filters">
     <div class="frow">
       <span class="flabel">Show</span>
       <div class="tabs">
         <button type="button" class="tab active" onclick="setStatus('all',this)">All</button>
         <button type="button" class="tab" onclick="setStatus('active',this)">Active</button>
         <button type="button" class="tab" onclick="setStatus('done',this)">Completed</button>
       </div>
       <span class="spacer"></span>
       <select class="sortsel" id="sort" onchange="render()">
         <option value="created">Sort: Newest</option>
         <option value="due">Sort: Due date</option>
         <option value="priority">Sort: Priority</option>
         <option value="alpha">Sort: A - Z</option>
       </select>
     </div>
     <div class="frow">
       <span class="flabel">Priority</span>
       <button type="button" class="fchip pf-high" onclick="togglePrio('high',this)">High</button>
       <button type="button" class="fchip pf-medium" onclick="togglePrio('medium',this)">Medium</button>
       <button type="button" class="fchip pf-low" onclick="togglePrio('low',this)">Low</button>
     </div>
     <div class="frow">
       <span class="flabel">Category</span>
       <div id="catFilters" class="frow" style="gap:.45rem;"></div>
       <span class="spacer"></span>
       <button type="button" class="clearf" id="clearf" style="display:none" onclick="clearFilters()">Clear filters</button>
     </div>
     <div class="frow">
       <input id="search" class="search" placeholder="Search tasks..." oninput="render()" style="flex:1;">
     </div>
   </div>

   <ul id="list"></ul>
   <div class="empty" id="empty">No tasks yet. Add one above.</div>
  </div>
 </div>
 <div class="foot">
   <span><span class="dot"></span>served by __POD__</span>
   <span><span id="count"></span> &nbsp; <button class="clearc" onclick="clearDone()">Clear completed</button></span>
 </div>

 <div class="modal" id="modal">
   <div class="mcard">
     <h3>Edit task</h3>
     <label>Title</label><input id="e_title">
     <label>Description</label><textarea id="e_desc" rows="2"></textarea>
     <div class="mrow">
       <div><label>Priority</label><select id="e_priority"><option value="low">Low</option><option value="medium">Medium</option><option value="high">High</option></select></div>
       <div><label>Due date</label><input id="e_due" type="date"></div>
     </div>
     <label>Category</label><select id="e_category"></select>
     <div class="mact"><button class="cancel" onclick="closeModal()">Cancel</button><button class="save" onclick="saveEdit()">Save changes</button></div>
   </div>
 </div>

 <script>
  var CATS=['Personal','Professional','Work','Study','Health','Finance','Shopping','Other'];
  var tasks=[], statusF='all', prioF=new Set(), catF=new Set(), editId=null;
  function esc(s){return (s||'').replace(/[&<>\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c];});}
  function hue(s){var h=0;s=s||'x';for(var i=0;i<s.length;i++){h=(h*31+s.charCodeAt(i))%360;}return h;}
  function catLight(s){var h=hue(s);return 'background:hsl('+h+',72%,93%);color:hsl('+h+',48%,34%);';}
  function catSolid(s){var h=hue(s);return 'background:hsl('+h+',55%,48%);color:#fff;border-color:transparent;';}
  function todayStr(){return new Date().toISOString().slice(0,10);}
  function fillSelect(id){var s=document.getElementById(id);s.innerHTML='';CATS.forEach(function(c){var o=document.createElement('option');o.value=c;o.textContent=c;s.appendChild(o);});}
  function prioRank(p){return p==='high'?0:(p==='medium'?1:2);}

  function setStatus(f,el){statusF=f;document.querySelectorAll('.tab').forEach(function(t){t.classList.remove('active');});el.classList.add('active');render();}
  function togglePrio(p,el){if(prioF.has(p)){prioF.delete(p);el.classList.remove('on');}else{prioF.add(p);el.classList.add('on');}render();}
  function toggleCat(c,el){if(catF.has(c)){catF.delete(c);el.classList.remove('on');el.setAttribute('style',catLight(c));}else{catF.add(c);el.classList.add('on');el.setAttribute('style',catSolid(c));}render();}
  function clearFilters(){prioF.clear();catF.clear();statusF='all';document.getElementById('search').value='';
    document.querySelectorAll('.tab').forEach(function(t,i){t.classList.toggle('active',i===0);});
    document.querySelectorAll('.pf-high,.pf-medium,.pf-low').forEach(function(c){c.classList.remove('on');});
    render();}

  function buildCatFilters(){
    var present={}; tasks.forEach(function(t){if(t.category)present[t.category]=1;});
    var cats=Object.keys(present).sort();
    catF.forEach(function(c){if(!present[c])catF.delete(c);});
    var box=document.getElementById('catFilters'); box.innerHTML='';
    if(!cats.length){var s=document.createElement('span');s.style.cssText='color:#cbd5e1;font-size:.75rem;';s.textContent='none yet';box.appendChild(s);return;}
    cats.forEach(function(c){
      var b=document.createElement('button'); b.type='button'; b.className='fchip'+(catF.has(c)?' on':''); b.textContent=c;
      b.setAttribute('style', catF.has(c)?catSolid(c):catLight(c));
      b.onclick=function(){toggleCat(c,b);};
      box.appendChild(b);
    });
  }

  function render(){
    buildCatFilters();
    var q=(document.getElementById('search').value||'').toLowerCase();
    var sort=document.getElementById('sort').value;
    var total=tasks.length, done=tasks.filter(function(t){return t.done;}).length;
    var pct= total? Math.round(done/total*100):0;
    document.getElementById('fill').style.width=pct+'%';
    document.getElementById('pctBig').textContent=pct+'%';
    document.getElementById('plabel').textContent= total? (done+' of '+total+' tasks completed') : 'No tasks yet';
    document.getElementById('count').textContent= total+' task'+(total===1?'':'s');
    var anyF = prioF.size||catF.size||statusF!=='all'||q;
    document.getElementById('clearf').style.display= anyF? 'inline':'none';

    var view=tasks.filter(function(t){
      if(statusF==='active'&&t.done)return false;
      if(statusF==='done'&&!t.done)return false;
      if(prioF.size&&!prioF.has(t.priority||'medium'))return false;
      if(catF.size&&!catF.has(t.category))return false;
      if(q){var hay=((t.title||'')+' '+(t.description||'')+' '+(t.category||'')).toLowerCase();if(hay.indexOf(q)<0)return false;}
      return true;
    });
    view.sort(function(a,b){
      if(sort==='priority')return prioRank(a.priority)-prioRank(b.priority)|| b.id-a.id;
      if(sort==='alpha')return (a.title||'').localeCompare(b.title||'');
      if(sort==='due'){var ad=a.due_date||'9999',bd=b.due_date||'9999';return ad<bd?-1:ad>bd?1:b.id-a.id;}
      return b.id-a.id;
    });

    var ul=document.getElementById('list');ul.innerHTML='';
    document.getElementById('empty').style.display= view.length? 'none':'block';
    document.getElementById('empty').textContent= total? 'No tasks match these filters.':'No tasks yet. Add one above.';
    var today=todayStr();
    view.forEach(function(t){
      var pr=t.priority||'medium';
      var dueCls='due'; if(t.due_date&&!t.done){if(t.due_date<today)dueCls='due over';else if(t.due_date===today)dueCls='due today';}
      var li=document.createElement('li');
      li.innerHTML=
        '<div class="cb '+(t.done?'on':'')+'" onclick="toggle('+t.id+','+(!t.done)+')">'+(t.done?'&#10003;':'')+'</div>'+
        '<div class="body">'+
          '<div class="title '+(t.done?'s':'')+'">'+esc(t.title)+'</div>'+
          (t.description?'<div class="desc">'+esc(t.description)+'</div>':'')+
          '<div class="badges">'+
            '<span class="pill p-'+pr+'">'+pr+'</span>'+
            (t.category?'<span class="pill" style="'+catLight(t.category)+'">'+esc(t.category)+'</span>':'')+
            (t.due_date?'<span class="pill '+dueCls+'">&#128197; '+esc(t.due_date)+(dueCls.indexOf("over")>=0?' (overdue)':'')+'</span>':'')+
          '</div>'+
        '</div>'+
        '<div class="actions">'+
          '<button class="ico" title="Edit" onclick="openEdit('+t.id+')">&#9998;</button>'+
          '<button class="ico del" title="Delete" onclick="del('+t.id+')">&#128465;</button>'+
        '</div>';
      ul.appendChild(li);
    });
  }

  async function addTask(e){e.preventDefault();
    var title=document.getElementById('title').value.trim(); if(!title)return false;
    var body={title:title,
      priority:document.getElementById('priority').value,
      category:document.getElementById('category').value,
      due_date:document.getElementById('due').value,
      description:document.getElementById('desc').value.trim()};
    await fetch('/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    document.getElementById('title').value='';document.getElementById('desc').value='';document.getElementById('due').value='';
    document.getElementById('priority').value='medium';
    load(); return false;}
  async function toggle(id,done){await fetch('/tasks/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({done:done})});load();}
  async function del(id){await fetch('/tasks/'+id,{method:'DELETE'});load();}
  async function clearDone(){var ids=tasks.filter(function(t){return t.done;}).map(function(t){return t.id;});
    for(var i=0;i<ids.length;i++){await fetch('/tasks/'+ids[i],{method:'DELETE'});} load();}
  function openEdit(id){var t=tasks.find(function(x){return x.id===id;});if(!t)return;editId=id;
    document.getElementById('e_title').value=t.title||'';
    document.getElementById('e_desc').value=t.description||'';
    document.getElementById('e_priority').value=t.priority||'medium';
    document.getElementById('e_due').value=t.due_date||'';
    var ec=document.getElementById('e_category'); if(t.category&&CATS.indexOf(t.category)<0){var o=document.createElement('option');o.value=t.category;o.textContent=t.category;ec.appendChild(o);} ec.value=t.category||'Personal';
    document.getElementById('modal').classList.add('show');}
  function closeModal(){document.getElementById('modal').classList.remove('show');editId=null;}
  async function saveEdit(){if(editId==null)return;
    var body={title:document.getElementById('e_title').value.trim(),
      description:document.getElementById('e_desc').value.trim(),
      priority:document.getElementById('e_priority').value,
      due_date:document.getElementById('e_due').value,
      category:document.getElementById('e_category').value};
    await fetch('/tasks/'+editId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    closeModal();load();}
  document.getElementById('modal').addEventListener('click',function(e){if(e.target.id==='modal')closeModal();});
  async function load(){var r=await fetch('/tasks');tasks=await r.json();render();}
  fillSelect('category'); fillSelect('e_category'); document.getElementById('category').value='Personal';
  load();
 </script>
</body>
</html>"""


@app.route('/')
def index():
    html = _PAGE.replace('__POD__', socket.gethostname())
    return html, 200, {'Content-Type': 'text/html'}



if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
