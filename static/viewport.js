function setVh() {
  const height = window.visualViewport ? window.visualViewport.height : window.innerHeight;
  const vh = height * 0.01;
  document.documentElement.style.setProperty('--vh', `${vh}px`);
}

window.addEventListener('resize', setVh);
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', setVh);
}

setVh();
