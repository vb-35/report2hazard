/* One upright turn every five seconds and on each pointer entry. */
(() => {
  const logo = document.querySelector(".brand-medallion");
  const mark = logo?.querySelector(".brand-mark");
  if (!mark) return;
  const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
  let animation;
  function spin() {
    if (reducedMotion.matches) return;
    animation?.cancel();
    animation = mark.animate([
      { transform: "scale(1) rotateY(0deg)", offset: 0 },
      { transform: "scale(1.42) rotateY(45deg)", offset: .2 },
      { transform: "scale(1.42) rotateY(315deg)", offset: .75 },
      { transform: "scale(1) rotateY(360deg)", offset: 1 },
    ], { duration: 1000, easing: "ease-in-out", iterations: 1 });
  }
  let timer = setInterval(spin, 5000);
  logo.addEventListener("pointerenter", () => {
    spin();
    clearInterval(timer);
    timer = setInterval(spin, 5000);
  });
  reducedMotion.addEventListener("change", () => {
    if (reducedMotion.matches) animation?.cancel();
  });
})();
