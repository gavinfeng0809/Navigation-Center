const fs=require('fs'), vm=require('vm'), path=require('path');
const PUB=path.join(__dirname,'..','public');

function makeEl(id){
  const cls={};
  const el={
    id, hidden:false, value:'', textContent:'', disabled:false, checked:false,
    style:{}, dataset:{}, children:[], offsetParent:{},
    innerHTML:'', outerHTML:'', classList:{add:c=>cls[c]=1,remove:c=>delete cls[c],
      toggle:c=>cls[c]?delete cls[c]:(cls[c]=1),contains:c=>!!cls[c]},
    addEventListener(){}, removeEventListener(){}, appendChild(){}, remove(){},
    setAttribute(){}, getAttribute(){return null}, removeAttribute(){},
    querySelector(){return null}, querySelectorAll(){return []},
    closest(){return null}, focus(){}, select(){}, scrollIntoView(){}, insertAdjacentHTML(){}
  };
  return el;
}
/* 真实 DOM 语义：只有 markup 里出现过的 id 才存在，其余返回 null。
   ⚠️ 这一点至关重要 —— 早期版本会自动创建任何元素，于是
   `document.getElementById('loginForm').addEventListener(...)` 这种
   「引用了本页不存在的元素」的错会被悄悄吞掉，测试全绿但用户看到的是白屏。
   导航中心白屏就是这么漏过去的。 */
function makeDoc(html){
  const KNOWN=new Set([...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]));
  const els={};
  return {
    _els:els, _known:KNOWN,
    getElementById(id){
      if(!KNOWN.has(id)) return null;          // ← 与浏览器一致
      if(!els[id]) els[id]=makeEl(id);
      return els[id];
    },
    querySelector(){ return makeEl('q'); },
    querySelectorAll(){ return []; },
    createElement(){ return makeEl('new'); },
    addEventListener(){}, removeEventListener(){},
    body: makeEl('body'), documentElement: makeEl('html'),
    activeElement: makeEl('ae')
  };
}

function runPage(file, label){
  const html=fs.readFileSync(path.join(PUB,file),'utf8');
  const re=/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let m, pageJs='';
  while((m=re.exec(html))) pageJs=m[1];
  const common=fs.readFileSync(path.join(PUB,'common.js'),'utf8');
  const doc=makeDoc(html);
  const store={};
  const sandbox={
    console, setTimeout, clearTimeout, setInterval, clearInterval, Promise, JSON, Math, Date,
    String, Number, Array, Object, RegExp, Error, encodeURIComponent, decodeURIComponent,
    Uint32Array, isNaN, parseInt, parseFloat, URL, URLSearchParams,
    FormData, Blob, File, Headers, Request, Response, AbortController,
    document:doc,
    location:{ href:'http://10.127.207.71:8091/'+file, origin:'http://10.127.207.71:8091',
               pathname:'/'+file, hash:'', search:'', replace(){}, reload(){} },
    navigator:{ userAgent:'node', clipboard:null },
    localStorage:{ getItem:k=>store[k]===undefined?null:store[k], setItem:(k,v)=>store[k]=String(v),
                   removeItem:k=>delete store[k] },
    sessionStorage:{ getItem:()=>null, setItem(){}, removeItem(){} },
    matchMedia:()=>({matches:false, addEventListener(){}}),
    fetch:(u)=>{
      const url=String(u);
      let body={user:null,items:[],groups:[],roles:[],total:0,online:0,offline:0,recent:[]};
      if(url.indexOf('/api/nav')>=0){
        /* 给一份有内容的导航数据：空数据会掩盖「整页渲染不出来」这类 bug */
        body=Object.assign(body,{total:3,online:1,groups:[{id:1,name:'测试分组',items:[
          {id:1,name:'甲系统',url:'https://a.example/',subgroup:'',launch:'web',favorite:1,visit_count:2},
          {id:2,name:'乙系统',url:'https://b.example/',subgroup:'',launch:'web',favorite:0,visit_count:0}
        ]},{id:2,name:'第二组',items:[
          {id:3,name:'丙系统',url:'https://c.example/',subgroup:'S1',launch:'web',favorite:0,visit_count:0}
        ]}]});
      } else if(url.indexOf('/api/session')>=0){
        body={user:{id:1,username:'tester',role:'viewer',role_label:'查看',display_name:'测试员',weak_pwd:0,enabled:1}};
      }
      return Promise.resolve({ ok:true, status:200, headers:{get:()=>'application/json'},
                                json:()=>Promise.resolve(body),
                                text:()=>Promise.resolve('{}'), blob:()=>Promise.resolve(null) });
    },
    XMLHttpRequest:function(){ this.open=()=>{}; this.send=()=>{}; this.setRequestHeader=()=>{}; },
    crypto:require('crypto').webcrypto,
    requestAnimationFrame:f=>setTimeout(f,0),
    addEventListener(){}, removeEventListener(){}, dispatchEvent(){return true},
    getComputedStyle:()=>({getPropertyValue:()=>''}),
    history:{pushState(){},replaceState(){},back(){}},
    scrollTo(){}, open(){},
    innerWidth:1440, innerHeight:900, isSecureContext:true,
    print(){}, close(){}, focus(){},
    alert(){}, confirm(){return true}, prompt(){return null}
  };
  sandbox.window=sandbox;
  sandbox.self=sandbox;
  sandbox.globalThis=sandbox;
  try{
    vm.createContext(sandbox);
    vm.runInContext(common, sandbox, {filename:'common.js'});
    if(!sandbox.window.NH) throw new Error('common.js 没有导出 window.NH');
    vm.runInContext(pageJs, sandbox, {filename:file});
    console.log(`  ✓ ${label}：common.js + 页面脚本加载并执行 boot() 无异常`);
    return {sandbox, doc};
  }catch(e){
    console.log(`  ✗ ${label}：${e.message}`);
    console.log('     ' + String(e.stack||'').split('\n').slice(1,4).join('\n     '));
    return null;
  }
}

/* 渲染结果检查：这是最要紧的一层。
   之前只有「不抛异常」这一条，而 renderPortal 抛错会被 loadNav 的 catch 吃掉，
   页面上只留一句「导航加载失败」——测试全绿但用户看到的是一片空白。 */
function checkRendered(ctx, label, expect){
  return new Promise(function(resolve){
    setTimeout(function(){
      let bad=[];
      for(const sel of expect){
        const el=ctx.doc._known.has(sel.id) ? ctx.doc._els[sel.id] : null;
        const got=el ? el.innerHTML : '';
        if(sel.min && got.length < sel.min) bad.push(sel.id+' 只有 '+got.length+' 字符');
        if(sel.not && got.indexOf(sel.not)>=0) bad.push(sel.id+' 含错误提示: '+sel.not);
      }
      if(bad.length){
        console.log(`  ✗ ${label} 渲染不完整：${bad.join('；')}`);
        resolve(false);
      } else {
        console.log(`  ✓ ${label} 渲染完整`);
        resolve(true);
      }
    }, 120);
  });
}

/**
 * 两页真实加载冒烟测试（零依赖，node tools/test_pages.js）
 *
 * 用 DOM 桩把 public/common.js 与两个页面的内联脚本**真的执行一遍**，
 * 并跑完各自的 boot()。这能抓到静态检查抓不到的问题：
 *   - common.js 导出了不存在的名字（NH.xxx = undefined）
 *   - 页面引用了没解构到的共用函数
 *   - boot() 里访问了不存在的元素而抛异常
 * 拆分后这类问题最容易出现，所以每次改前端都该跑一遍。
 */
console.log('=== 两页真实加载 + 渲染测试 ===');
const a=runPage('index.html','导航中心 /');
const b=runPage('notices.html','通知中心 /notices');
let okA=true, okB=true;
if(!a||!b){
  okA=!!a; okB=!!b;
} else {
  Promise.resolve()
    .then(()=>checkRendered(a,'导航中心 /',[
      {id:'navSide', min:100},                    // 左侧分组标题栏必须有内容
      {id:'navGroups', min:100, not:'导航加载失败'}, // 右栏必须有内容且不是错误提示
      {id:'navGroups', not:'还在加载'}
    ]))
    .then(r=>{ okA=r; return checkRendered(b,'通知中心 /notices',[{id:'app', min:0}]); })
    .then(r=>{ okB=r; })
    .then(()=>{
      const all=okA&&okB;
      console.log(all ? '\n全部通过 ✓' : '\n有失败 ✗');
      process.exit(all?0:1);
    });
}
