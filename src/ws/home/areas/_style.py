"""The home page's own rules, after the shared ones in `ws.ui.css`: the app shell, the box, the list, the menus.

Colours are `var(--role)` from `ws.brand`, so light and dark, and the accent a person picks (`data-accent`), follow the
tokens; there is no `style=` anywhere and no outside font. Two things change meaning with a choice the person makes and
both are attributes on `<html>` or `#list`, set by the page's script from localStorage: the look (`data-theme`,
`data-accent`, `data-font`, `data-size`) and the view (`data-group`, `data-density`, `data-show-*`).
"""

from __future__ import annotations

from ws.brand import colour
from ws.ui import css

__all__ = ["CSS", "PAGES"]

# The places the sidebar reaches, as (id, label). `home` and `evals` are drawn; the rest say what is coming.
PAGES = ("home", "evals", "sources")


def _violet() -> str:
    """`--violet`: the brand accent as it is before a person picks another one, for the swatch that stands for it."""
    dark, light = colour("accent", "dark"), colour("accent", "light")
    return (
        f":root{{--violet:{dark}}}\n:root[data-theme=light]{{--violet:{light}}}\n"
        f"@media(prefers-color-scheme:light){{:root:not([data-theme=dark]){{--violet:{light}}}}}\n"
    )


CSS = (
    css.PANEL
    + _violet()
    + """
:root{--faint:color-mix(in srgb,var(--muted) 72%,var(--bg));--warn-soft:color-mix(in srgb,var(--warn) 9%,transparent);
--serif:ui-serif,Georgia,"Times New Roman",serif;--sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--list-font:var(--sans);--size:15px}
:root[data-accent=indigo]{--accent:var(--indigo);--accent-text:var(--indigo)}
:root[data-accent=teal]{--accent:var(--teal);--accent-text:var(--teal)}
:root[data-font=mono]{--list-font:var(--mono)}
:root[data-size=s]{--size:13.5px}:root[data-size=l]{--size:16.5px}
body{background:var(--bg);font-size:var(--size);-webkit-font-smoothing:antialiased}
[hidden]{display:none!important}
.sprite{position:absolute}
svg.i{width:16px;height:16px;flex:none;fill:none;stroke:currentColor;stroke-width:1.7;stroke-linecap:round;
stroke-linejoin:round}
.num{font-variant-numeric:tabular-nums}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* the shell: a sidebar of places, one quiet column */
.app{display:grid;grid-template-columns:268px minmax(0,1fr);min-height:100vh}
@media (min-width:601px){.app{background:linear-gradient(90deg,var(--panel) 267px,var(--line) 267px 268px,transparent 268px)}}
.side{position:sticky;top:0;height:100vh;overflow-y:auto;border-right:1px solid var(--line);background:var(--panel);
display:flex;flex-direction:column;padding:14px 12px;gap:2px}
.sbrand{display:flex;align-items:center;gap:10px;padding:4px 8px 12px}
.sbrand .logo{margin:0;font:700 5px/5px var(--mono);white-space:pre;letter-spacing:0}
.sbrand .logo i{font-style:normal}
.sbrand b{font-size:15px;font-weight:650}
.nav{display:flex;align-items:center;gap:10px;padding:7px 8px;border-radius:8px;color:var(--text);font-size:14px;
width:100%;text-align:left;text-decoration:none;border:0;background:none;cursor:pointer}
.nav:hover{background:var(--raise)}
.nav[aria-current=page],.nav.on{background:var(--raise);font-weight:550}
.nav.new{border:1px solid var(--line2);margin-bottom:6px;padding:8px}
.nav .n{margin-left:auto;font-size:12px;font-weight:600;color:var(--warn)}
.nav .sub{margin-left:auto;font-size:12px;color:var(--faint);white-space:nowrap}
.side h6{font:500 12px/1 var(--sans);color:var(--faint);margin:16px 8px 6px}
.ws-dot{width:7px;height:7px;border-radius:2px;background:var(--line2);flex:none}
.foot{margin-top:auto;display:flex;align-items:center;gap:10px;padding:10px 8px 2px;border-top:1px solid var(--line)}
.avatar{width:26px;height:26px;border-radius:50%;background:var(--raise);display:grid;place-content:center;
font:600 11px var(--sans);border:1px solid var(--line2);flex:none}
.who{font-size:13px;line-height:1.25;min-width:0;overflow-wrap:anywhere}.who span{display:block;color:var(--faint);font-size:12px}
.foot .nav{width:auto;margin-left:auto;padding:6px}
.side summary.h6{font:500 12px/1 var(--sans);color:var(--faint);margin:16px 8px 6px;cursor:pointer;list-style:none}
.side summary.h6::-webkit-details-marker{display:none}
button.avatar{cursor:pointer;color:var(--text)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.scrim{display:none}
.sscrim,.plus,.shead,.pick .pl,.topbar .ttl,.ph{display:none}
.picks{display:contents}
.main{min-width:0;display:flex;flex-direction:column}
.topbar{position:relative;display:flex;align-items:center;justify-content:flex-end;gap:8px;padding:12px 20px;min-height:52px}
.topbar .menu{display:none;margin-right:auto}
.pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);border-radius:8px;padding:5px 10px;
font-size:13px;color:var(--muted);background:var(--panel);cursor:pointer}
.pill:hover{color:var(--text);border-color:var(--line2)}
.pill select{appearance:none;border:0;background:none;color:inherit;font:inherit;cursor:pointer;padding:0;max-width:16ch}
.pill select option{color:#000}
.col{width:min(760px,100%);margin:0 auto;display:flex;flex-direction:column;gap:22px;padding:0 20px 56px}
.pg:not(#v-home){display:none}
#v-home,.pg:not(#v-home):target{display:flex;flex-direction:column;gap:22px}
.col:has(.pg:not(#v-home):target) #v-home,body:has(.pg:not(#v-home):target) .only-home{display:none}
.hello{font:400 30px/1.2 var(--serif);text-align:center;margin:20px 0 0;letter-spacing:-.01em}
.vh2{font:600 22px/1.25 var(--sans);margin:18px 0 0;letter-spacing:-.01em}
.soon{border:1px dashed var(--line2);border-radius:14px;padding:22px;color:var(--muted);display:grid;gap:8px}
.soon b{color:var(--text);font-weight:550}
.back{color:var(--accent-text);text-decoration:none}.back:hover{text-decoration:underline}

/* the box: describe it, then four pickers */
.composer{border:1px solid var(--line2);border-radius:16px;background:var(--panel);padding:14px 14px 10px;display:grid;
gap:10px;box-shadow:0 1px 0 var(--line)}
.composer textarea{width:100%;min-height:48px;resize:vertical;border:0;background:none;color:var(--text);
font:inherit;font-size:15px;padding:0;outline:none}
.composer textarea::placeholder{color:var(--faint)}
.crow{display:flex;align-items:center;gap:2px;flex-wrap:wrap}
.pick{display:inline-flex;align-items:center;gap:6px;padding:4px 6px;border-radius:8px;font-size:13px;color:var(--muted);
border:1px solid transparent;position:relative;min-width:0}
.pick:hover,.pick:focus-within{background:var(--raise);color:var(--text)}
.pick select{appearance:none;border:0;background:none;color:inherit;font:inherit;cursor:pointer;padding:0 14px 0 0;
max-width:18ch;text-overflow:ellipsis;outline:none}
.pick:last-of-type select{max-width:24ch}
.pick select option,.pick select optgroup{color:#000;background:#fff}
.pick .chev{position:absolute;right:6px;width:12px;height:12px;opacity:.7;pointer-events:none}
.send{margin-left:auto;width:32px;height:32px;border-radius:9px;background:var(--accent);color:var(--on-accent);
display:grid;place-content:center}
.send:disabled{background:var(--raise);color:var(--faint);cursor:default}
.cnote{margin:0;font-size:12.5px;color:var(--muted)}
.cnote.bad{color:var(--warn)}
.cnote.good{color:var(--ok)}

/* the list: groups of rows, a row a <details> */
.listhead{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted);flex-wrap:wrap}
.listhead .sum{margin-right:auto}
.grp{display:grid}
.grp>h5,.grp>summary{margin:14px 0 4px;font:500 13px/1 var(--sans);color:var(--muted);display:flex;gap:8px;
align-items:baseline;list-style:none;cursor:default}
.grp>summary{cursor:pointer}.grp>summary::-webkit-details-marker{display:none}
.grp>summary::before{content:"›";display:inline-block;width:10px;transition:none}
.grp[open]>summary::before{content:"⌄"}
.grp h5 em,.grp summary em{font-style:normal;color:var(--faint);font-weight:400}
.r{margin:0 -10px;border-radius:10px;font-family:var(--list-font)}
.r:hover{background:var(--raise)}
.r>summary{display:grid;grid-template-columns:18px minmax(0,1fr) auto;gap:2px 10px;align-items:start;padding:9px 10px;
list-style:none;cursor:pointer}
.r>summary::-webkit-details-marker{display:none}
.rg{text-align:center;line-height:21px}
.rg .st{display:inline}.rg .g{font-size:13px}
.rg .w{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.rt{min-width:0;display:grid;gap:1px}
.l1{display:flex;gap:10px;min-width:0;align-items:baseline}
.nm{font-weight:550;white-space:nowrap}
.wh{color:var(--muted);overflow-wrap:anywhere;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.r.need .wh{color:var(--text)}
.l2{font-size:12.5px;color:var(--faint);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.l2 .sep{opacity:.6}
.l2 .mp{display:inline-flex;gap:6px;align-items:center}
.vshort{display:none}
.l2 .sw{display:none}
#list:not([data-group=need]) .l2 .sw{display:inline-flex;gap:6px}
.age{font-size:12.5px;color:var(--faint);line-height:21px;white-space:nowrap}
.tagc{font-size:11.5px;border:1px solid var(--line2);border-radius:5px;padding:0 5px;color:var(--muted)}
.r.done{opacity:.68}
.rb{padding:2px 10px 12px 38px;display:grid;gap:8px;font-family:var(--sans);min-width:0;overflow-wrap:anywhere}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:13px;margin:0}
.kv dt{color:var(--muted)}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.more{font-size:13px;color:var(--muted)}
#list[data-density=compact] .l2{display:none}
#list[data-density=compact] .r>summary{padding:5px 10px}
#list[data-show-pinned="0"] [data-grp=pin],#list[data-show-done="0"] [data-grp=done],
#list[data-show-model="0"] .mdl{display:none}
#list[data-group=workspace] [data-grp=pin],#list[data-group=model] [data-grp=pin],#list[data-group=machine] [data-grp=pin]{display:none}
.col[data-show-quiet="0"] .quiet{display:none}

/* an ask, opened in place */
.ask{display:grid;gap:8px}
.ask .q{margin:0;font-weight:550}
.dec{font-size:12.5px;color:var(--muted);margin:0}
.opts2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:8px}
.opt{display:grid;gap:1px;text-align:left;border:1px solid var(--line2);border-radius:10px;padding:9px 12px;
background:var(--panel);min-height:44px}
.opt b{font-weight:550;display:flex;justify-content:space-between;gap:10px}
.opt span{font-size:12.5px;color:var(--muted)}
.opt.rec{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.opt.rec span{color:color-mix(in srgb,var(--on-accent) 78%,transparent)}
.opt small{font-weight:400;font-size:12px;opacity:.85;white-space:nowrap;flex:none}
.opt:disabled{opacity:.6;cursor:default}
.else{display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--line2);padding:6px 2px}
.else input{flex:1;min-width:0;border:0;background:none;color:var(--text);font:inherit;font-size:14px;outline:none;padding:2px 0}
.else input::placeholder{color:var(--faint)}
.else button{color:var(--muted);font-size:13px}
.links{display:flex;gap:16px;font-size:13px;color:var(--muted);flex-wrap:wrap;align-items:center}
.links button{color:inherit}.links button:hover{color:var(--text)}
.stamp{display:flex;gap:10px;align-items:center;padding:10px 12px;border-radius:10px;background:var(--raise);font-size:13.5px}
.stamp .ok{color:var(--ok)}.stamp .bad{color:var(--warn)}
.stamp button{margin-left:auto;color:var(--accent-text);font-weight:550}
.rec-tag{color:var(--ok);font-size:12px;margin-left:8px;font-weight:550}
.opts{list-style:none;margin:0;padding:0;display:grid;gap:3px;font-size:13.5px}
.opts .d{display:block;color:var(--muted);font-size:13px}

/* the open button, the Claude app link and the line to paste */
.acts{display:flex;flex-wrap:wrap;gap:8px}
.act{border:1px solid var(--line2);border-radius:999px;padding:3px 12px;font-size:13px;color:var(--text);
text-decoration:none;white-space:nowrap;background:none;cursor:pointer}
.act:hover{border-color:var(--accent);background:var(--raise)}
.act:disabled{opacity:.6;cursor:default}
.act.off{color:var(--muted)}
.act.sm{padding:1px 8px;font-size:12px;color:var(--muted)}
.o-desk,.o-phone{display:inline-flex;flex-wrap:wrap;gap:8px;align-items:center}
.o-phone{display:none}
.why{color:var(--muted);font-size:12px}
.chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 9px;font-size:12px;line-height:20px;
color:var(--muted);overflow-wrap:anywhere}

/* the quiet line under the list */
.quiet{display:flex;gap:6px 22px;flex-wrap:wrap;font-size:13px;color:var(--muted);border-top:1px solid var(--line);
padding-top:14px}
.quiet a{color:inherit;text-decoration:none}.quiet a:hover{color:var(--text)}
.quiet b{font-weight:550;color:var(--text)}
.empty{margin:0;padding:16px 18px;color:var(--muted)}
.list{list-style:none;margin:0;padding:0}

/* menus: View and Settings */
.pop{position:absolute;right:20px;top:50px;width:min(320px,calc(100vw - 24px));background:var(--panel);
border:1px solid var(--line2);border-radius:12px;box-shadow:0 12px 32px rgba(0,0,0,.25);padding:12px;display:grid;gap:12px;
z-index:20;font-size:13px}
.pop.set{position:fixed;left:12px;right:auto;top:auto;bottom:58px}
.pop h6{margin:0 0 6px;font:500 12px var(--sans);color:var(--faint)}
.seg{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;background:var(--raise);border-radius:8px;padding:2px;gap:2px}
.seg button{padding:5px 6px;border-radius:6px;font-size:12.5px;color:var(--muted)}
.seg button.on{background:var(--panel);color:var(--text);box-shadow:0 0 0 1px var(--line2)}
.chk{display:flex;align-items:center;gap:8px;padding:3px 0;color:var(--text);cursor:pointer;width:100%;text-align:left}
.box{width:15px;height:15px;border-radius:4px;border:1px solid var(--line2);display:grid;place-content:center;flex:none}
.box svg{width:11px;height:11px;stroke-width:2.6;visibility:hidden}
.chk[aria-checked=true] .box{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.chk[aria-checked=true] .box svg{visibility:visible}
.sw{display:flex;gap:8px}
.sw button{width:22px;height:22px;border-radius:50%;border:2px solid var(--panel);box-shadow:0 0 0 1px var(--line2)}
.sw button.on{box-shadow:0 0 0 2px var(--text)}
.sw .c-v{background:var(--violet)}.sw .c-i{background:var(--indigo)}.sw .c-t{background:var(--teal)}
.popfoot{display:flex;gap:8px;align-items:center;border-top:1px solid var(--line);padding-top:10px;color:var(--muted)}
.popfoot input{flex:1;min-width:0;border:1px solid var(--line2);border-radius:8px;background:var(--bg);color:var(--text);
font:inherit;font-size:13px;padding:5px 8px}
.btn{border:1px solid var(--line2);border-radius:8px;padding:5px 10px;font-size:13px}
.pop a{color:var(--muted);text-decoration:none}.pop a:hover{color:var(--text)}

/* the Evals and Sources pages */
.fold>summary{cursor:pointer;padding:11px 18px;color:var(--muted);border-top:1px solid var(--line)}
.fold>summary:hover{color:var(--text)}
.arms{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:14px;padding:16px 18px}
.arm{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.arm header{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.arm h3{margin:0;font-size:16px;font-weight:650}
.job{color:var(--muted);font-size:12.5px;font-family:var(--mono)}
.rate{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:8px 0 2px}
.big{font-size:30px;line-height:1.2;font-weight:650;letter-spacing:-.015em;font-variant-numeric:tabular-nums}
.big.off{font-size:18px;color:var(--muted)}
.of{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;margin:14px 0}
.stat{display:flex;flex-direction:column;min-width:0}
.stat .k{color:var(--muted);font-size:12.5px}
.stat .v{font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.stat .n{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.trend{display:flex;align-items:center;gap:12px}
.arm .spark{width:160px;height:32px}
.cap{color:var(--muted);font-size:12.5px}
.band{margin:10px 0 0;color:var(--muted);font-size:12.5px}
.gap{margin:0;padding:0 18px 16px}
.srcs{padding:4px 0 8px}
.srcs>li{display:grid;grid-template-columns:132px 150px minmax(0,1fr);gap:4px 18px;padding:5px 18px;font-size:13px}
.srcs .nm{font-weight:500;font-family:var(--mono)}
.srcs .why{color:var(--muted);overflow-wrap:anywhere}
.panel>details>summary{cursor:pointer;display:flex;justify-content:space-between;padding:12px 18px;font-weight:600;
font-size:14px;list-style:none}
.panel>details>summary::-webkit-details-marker{display:none}
.panel>details>summary span{color:var(--muted);font-weight:400;font-size:13px}

/* a phone: a top bar with the menu, two-line rows, one pill fixed at the bottom and a sheet for the pickers */
@media (max-width:600px){
:root{--size:16px}
.app{grid-template-columns:minmax(0,1fr)}
.side{position:fixed;z-index:40;inset:0 auto 0 0;width:min(340px,88vw);transform:translateX(-102%);height:100dvh;
padding:max(8px,env(safe-area-inset-top)) 12px 0;gap:0}
body.drawer .side{transform:none;box-shadow:0 0 40px rgba(0,0,0,.4)}
body.drawer .scrim{display:block;position:fixed;inset:0;z-index:30;background:rgba(0,0,0,.4)}
.side .dk{display:none}
.side h6.ph{display:block;font-size:15px;margin:20px 12px 6px;color:var(--muted)}
.sbrand{padding:6px 4px 14px;gap:12px}
.sbrand .logo{font-size:5px}
.sbrand b{font:400 30px/1 var(--serif);letter-spacing:-.01em;flex:1}
.rbtn.ph{display:grid;place-content:center;width:48px;height:48px;border-radius:50%;background:var(--raise);
border:1px solid var(--line2);color:var(--text)}
.rbtn svg.i{width:22px;height:22px}
.nav{font-size:17px;gap:14px;padding:0 12px;min-height:52px;border-radius:14px}
.nav svg.i{width:22px;height:22px}
.nav .n,.nav .sub{font-size:15px}
.sessions.ph{display:grid}
.sess{min-height:48px;font-size:16px;gap:12px}
.sess .rg{width:22px;line-height:1}.sess .rg .g{font-size:16px}
.sess .sn{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nav.more{min-height:44px;color:var(--muted);font-size:16px}
.wsd>summary.h6{display:flex;align-items:center;gap:8px;min-height:44px;margin:20px 12px 4px;font-size:15px;
color:var(--muted);cursor:pointer;list-style:none}
.wsd>summary.h6::-webkit-details-marker{display:none}
.wsd>summary.h6::after{content:"›"}.wsd[open]>summary.h6::after{content:"⌄"}
.foot{position:sticky;bottom:0;margin-top:auto;background:var(--panel);border-top:0;gap:12px;
padding:12px 4px calc(12px + env(safe-area-inset-bottom))}
.foot .who,.foot .gear{display:none}
.avatar{width:48px;height:48px;font-size:15px;color:var(--text)}
.nav.new.ph{display:flex;flex:1;margin-left:auto;max-width:240px;justify-content:center;border-radius:999px;border:0;
background:var(--text);color:var(--bg);font-weight:550;min-height:52px;width:auto;padding:0 22px}
.nav.new.ph:hover{background:var(--text)}
.topbar{position:sticky;top:0;z-index:15;background:var(--bg);padding:8px 12px;gap:8px;min-height:56px}
.topbar{justify-content:space-between}
.topbar .menu{display:inline-flex;margin-right:0;border-radius:50%;width:44px;padding:0}
.topbar .ttl{display:block;position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);font-size:18px;
font-weight:650;letter-spacing:-.01em;pointer-events:none}
.topbar .pill{min-height:44px;min-width:44px;justify-content:center;font-size:15px;padding:6px 12px}
.col{padding:0 0 calc(160px + env(safe-area-inset-bottom));gap:0}
#v-home,.pg:not(#v-home):target{gap:0}
.pg:not(#v-home):target{padding:0 16px}
.hello,.listhead{display:none}
.grp>h5,.grp>summary{margin:18px 16px 4px;font:600 15px/1.2 var(--sans);min-height:28px;align-items:center}
.grp>summary{min-height:44px;margin-top:6px}
.grp>.more{margin:6px 16px;font-size:15px}
.r{margin:0;border-radius:0}
.r>summary{position:relative;grid-template-columns:16px minmax(0,1fr) auto;gap:1px 10px;padding:9px 16px;align-items:baseline}
.r>summary::after{content:"";position:absolute;left:42px;right:0;bottom:0;border-bottom:1px solid var(--line)}
.rt,.l1{display:contents}
.rg{grid-area:1/1;line-height:22px}
.nm{grid-area:1/2;font-size:16px;line-height:22px;overflow:hidden;text-overflow:ellipsis}
.age{grid-area:1/3;font-size:15px;line-height:22px}
.wh,.l2{grid-column:2/4;grid-row:2}
.l2{display:block;font-size:15px;line-height:1.35;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.l2 .mp{display:inline}.l2 .mp.x{display:none}
.l2 .sep{margin:0 6px}
#list:not([data-group=need]) .l2 .sw{display:inline}
.wh{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;white-space:normal;overflow:hidden;
font-size:15px;line-height:1.35;color:var(--muted)}
.r.need .wh{color:var(--muted)}
.r.need .l2{display:none}
.r:not(.need) .wh{display:none}
.rb{padding:6px 16px 16px}
.kv{font-size:15px}
.o-desk{display:none}
.o-phone{display:inline-flex}
.opts2{grid-template-columns:1fr}
.opt{min-height:52px;padding:12px 14px;font-size:16px}
.opt span,.dec{font-size:15px}
.tagc{font-size:14px}.why,.rec-tag,.opt small{font-size:14px}
.else{min-height:48px}.else input{font-size:16px;min-height:40px}
.else button,.links button,.act{min-height:44px;font-size:15px;display:inline-flex;align-items:center}
.ask .q{font-size:16px}
.stamp{font-size:15px;min-height:44px}
.quiet{margin:20px 16px 0;font-size:15px}
.quiet a{min-height:44px;display:inline-flex;align-items:center;gap:6px}
.topbar #viewlbl{display:none}.topbar .vshort{display:inline}
.list>li,.srcs>li{grid-template-columns:minmax(0,1fr)}
.arms{padding:14px}
.panel h2,.panel>details>summary,.empty,.fold>summary,.gap{padding-left:14px;padding-right:14px}
.vh2{margin:14px 0 0}
/* the box: the request, then a + for the sheet, the model and send, pinned to the bottom of the screen */
.composer{position:fixed;left:10px;right:10px;bottom:calc(8px + env(safe-area-inset-bottom));z-index:12;
grid-template-columns:44px minmax(0,1fr) 44px;grid-template-rows:auto 44px auto;align-items:center;gap:4px 8px;
padding:10px 10px 8px 12px;border-radius:24px;box-shadow:0 6px 24px rgba(0,0,0,.28)}
.composer textarea{grid-area:1/1/2/4;min-height:44px;max-height:140px;field-sizing:content;resize:none;
font-size:16px;line-height:24px;padding:10px 2px 8px;box-sizing:border-box}
.plus{grid-area:2/1;display:grid;place-content:center;width:44px;height:44px;border-radius:50%;
background:var(--raise);color:var(--text)}
.plus svg{width:22px;height:22px}
.crow{display:contents}
.crow>.pick{grid-area:2/2;justify-self:end;width:auto;max-width:100%;min-height:44px;padding:0 14px 0 12px;
border-radius:999px;background:var(--raise);color:var(--text);font-size:15px;gap:8px}
.crow>.pick select{max-width:none;height:44px;font-size:15px;padding-right:18px;min-width:0}
.crow>.pick:last-of-type select{max-width:none}
.crow>.pick .chev{right:10px}
.send{grid-area:2/3;margin:0;width:44px;height:44px;border-radius:50%}
.cnote{grid-area:3/1/4/4;margin:0;padding:2px 6px 0;font-size:15px}
.cnote:empty{display:none}
.picks{display:none;position:fixed;left:0;right:0;bottom:0;z-index:50;flex-direction:column;gap:8px;
background:var(--panel);border-radius:20px 20px 0 0;padding:14px 16px calc(16px + env(safe-area-inset-bottom));
box-shadow:0 -8px 32px rgba(0,0,0,.35)}
body.sheet .composer{z-index:60}
body.sheet .picks{display:flex}
body.sheet .sscrim{display:block;position:fixed;inset:0;z-index:45;background:rgba(0,0,0,.4)}
.shead{position:relative;display:flex;align-items:center;justify-content:center;font-size:17px;min-height:48px;
padding-top:10px}
.shead::before{content:"";position:absolute;top:0;left:50%;width:44px;height:5px;margin-left:-22px;border-radius:3px;
background:var(--line2)}
.shead .xbtn{position:absolute;left:0;bottom:2px;width:44px;height:44px;border-radius:50%;display:grid;
place-content:center;border:1px solid var(--line2);background:var(--panel);color:var(--text)}
.shead .xbtn svg{width:20px;height:20px}
.pick{display:flex;width:100%;min-height:52px;padding:4px 12px;border:1px solid var(--line);border-radius:12px;
font-size:16px;color:var(--text);gap:10px;align-items:center}
.picks .pick .pl{display:inline;color:var(--muted);width:7.6em;flex:none}
.picks .pick select{flex:1;min-width:0;max-width:none;height:46px;font-size:16px;padding-right:22px}
.picks .pick .chev{right:12px}
/* menus sit above the drawer and clear of the pill */
.pop{right:12px;font-size:15px}
.pop.set{z-index:60;left:12px;bottom:72px;width:min(320px,calc(100vw - 24px))}
.seg button{min-height:44px;font-size:15px}
.chk{min-height:44px}
.popfoot input{font-size:16px;min-height:44px}
.btn{min-height:44px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""
)
