/* Touch bridge for the topology canvas: app.js listens to mouse events only,
   so one-finger drag / pinch did nothing on phones and tablets. This turns
   touches into the mouse/wheel events app.js already understands.
   Does not modify app.js or any state. */
(function () {
  "use strict";
  function ready(fn){document.readyState==="loading"?document.addEventListener("DOMContentLoaded",fn):fn();}
  function fire(type, target, t, extra) {
    var e = new MouseEvent(type, Object.assign({bubbles:true,cancelable:true,view:window,
      clientX:t.clientX,clientY:t.clientY,button:0,buttons:type==="mouseup"?0:1}, extra||{}));
    target.dispatchEvent(e);
  }
  ready(function () {
    var box = document.getElementById("topology-canvas-container");
    if (!box || !("ontouchstart" in window)) return;
    var pinch = null, dragging = false;
    function dist(a,b){return Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);}
    box.addEventListener("touchstart", function (e) {
      if (e.target.closest && e.target.closest("button,a,input,select,label")) return; // canvas controls keep normal tap behaviour
      if (e.touches.length === 2) {
        if (dragging) { fire("mouseup", window, e.touches[0]); dragging = false; }
        pinch = dist(e.touches[0], e.touches[1]); return;
      }
      dragging = true;
      fire("mousedown", e.target, e.touches[0]);
    }, {passive:true});
    box.addEventListener("touchmove", function (e) {
      e.preventDefault();
      if (e.touches.length === 2 && pinch) {
        var d = dist(e.touches[0], e.touches[1]);
        var mid = {clientX:(e.touches[0].clientX+e.touches[1].clientX)/2,
                   clientY:(e.touches[0].clientY+e.touches[1].clientY)/2};
        box.dispatchEvent(new WheelEvent("wheel",{bubbles:true,cancelable:true,
          clientX:mid.clientX,clientY:mid.clientY,deltaY:(pinch-d)*2}));
        pinch = d; return;
      }
      if (dragging) fire("mousemove", window, e.touches[0]);
    }, {passive:false});
    function end(e){
      if (dragging) { fire("mouseup", window, (e.changedTouches||[{clientX:0,clientY:0}])[0]); dragging = false; }
      if (!e.touches || e.touches.length < 2) pinch = null;
    }
    box.addEventListener("touchend", end, {passive:true});
    box.addEventListener("touchcancel", end, {passive:true});
  });
})();
