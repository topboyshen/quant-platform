/* Navigation only. No data access or business actions. */
"use strict";
(() => {
  const sidebar = document.getElementById("sidebar");
  const toggle = document.getElementById("menu-toggle");
  if (!sidebar || !toggle) return;
  const mobile = window.matchMedia("(max-width: 991px)");
  function setNav(requested) {
    const open = Boolean(requested && mobile.matches);
    sidebar.classList.toggle("open", open);
    sidebar.inert = mobile.matches && !open;
    sidebar.setAttribute("aria-hidden", String(sidebar.inert));
    document.querySelector(".workspace-main").inert = open;
    document.getElementById("nav-backdrop").hidden = !open;
    document.body.style.overflow = open ? "hidden" : "";
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "关闭导航" : "打开导航");
    if (open) document.getElementById("nav-close").focus();
  }
  window.NNG8WorkbenchNav = { close: () => setNav(false) };
  toggle.addEventListener("click", () => setNav(!sidebar.classList.contains("open")));
  for (const id of ["nav-close", "nav-backdrop"]) document.getElementById(id).addEventListener("click", () => { setNav(false); toggle.focus(); });
  mobile.addEventListener("change", () => setNav(false));
  document.addEventListener("keydown", event => {
    if (!sidebar.classList.contains("open")) return;
    if (event.key === "Escape") { setNav(false); toggle.focus(); }
    if (event.key === "Tab") {
      const items = [...sidebar.querySelectorAll("a[href],button")];
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  setNav(false);
  // A source link must reveal its target inside the existing collapsed list.
  function revealSource() {
    if(document.body.dataset.page!=="report" || !/^#source-\d{1,8}$/.test(location.hash))return;
    const view=document.getElementById("view"); if(!view)return;
    let observer;
    const reveal=()=>{ const target=document.getElementById(location.hash.slice(1));if(!target)return false;
      for(let parent=target.parentElement;parent;parent=parent.parentElement)if(parent.tagName==="DETAILS")parent.open=true;
      target.scrollIntoView({block:"center"});observer?.disconnect();return true; };
    if(!reveal()){observer=new MutationObserver(reveal);observer.observe(view,{childList:true,subtree:true});setTimeout(()=>observer.disconnect(),15000);}
  }
  document.addEventListener("DOMContentLoaded",revealSource);
  window.addEventListener("hashchange",revealSource);
})();
