const slideTitles = [
  "Live LLM Casting",
  "What is casting for games?",
  "Casting example",
  "In-Game Casters",
  "Cloned Voices",
  "Casting example",
  "Restrained commentary",
  "Repetitive commentary",
  "Limited nuance",
  "Addressing the problems",
  "What could this look like?",
  "My pipeline: GSI",
  "My pipeline: Filter and prompt",
  "My pipeline: Output JSON",
  "My pipeline: TTS queue",
  "Voices cloned",
  "Live demonstration",
  "What I learned: Small models",
  "What I learned: Voice cloning",
  "What I learned: Live analysis",
  "Goals and target hardware",
  "Hardware used",
  "Text model VRAM",
  "TTS model VRAM",
  "Combined VRAM",
  "Latency target",
  "Measured latency",
  "Latency results",
  "Commentary goals",
  "Goal assessment",
  "More time: Server approach",
  "More time: Network simulation",
  "More time: Training data",
  "More time: Larger models",
  "More time: TTS fine tuning",
  "The Ethics: Voice cloning",
  "The Ethics: Impersonation",
  "The Ethics: Accuracy",
  "The Ethics: Consent",
  "The Ethics: Disclosure",
  "Thank You",
  "Pipeline detailed",
  "GSI and Filtering",
  "TTS",
  "Fine tuning"
];

const totalSlides = slideTitles.length;
const slideImage = document.querySelector("#slide-image");
const slideCaption = document.querySelector("#slide-caption");
const currentSlideLabel = document.querySelector("#current-slide");
const range = document.querySelector("#slide-range");
const previousButtons = [document.querySelector("#previous-button"), document.querySelector("#previous-edge")];
const nextButtons = [document.querySelector("#next-button"), document.querySelector("#next-edge")];
const overview = document.querySelector("#overview");
const overviewButton = document.querySelector("#overview-button");
const closeOverviewButton = document.querySelector("#close-overview");
const thumbnailGrid = document.querySelector("#thumbnail-grid");
const fullscreenButton = document.querySelector("#fullscreen-button");
let lastFocusedElement = null;
let touchStartX = null;

function slideFromLocation() {
  const match = window.location.hash.match(/slide=(\d+)/);
  const value = match ? Number(match[1]) : 1;
  return Math.min(totalSlides, Math.max(1, Number.isFinite(value) ? value : 1));
}

let currentSlide = slideFromLocation();

function preloadSlide(number) {
  if (number < 1 || number > totalSlides) return;
  const image = new Image();
  image.src = `./slides/slide-${number}.png`;
}

function renderSlide(updateHash = true) {
  const title = slideTitles[currentSlide - 1];
  slideImage.src = `./slides/slide-${currentSlide}.png`;
  slideImage.alt = `Slide ${currentSlide}: ${title}`;
  slideCaption.textContent = `Slide ${currentSlide} of ${totalSlides}: ${title}`;
  currentSlideLabel.textContent = String(currentSlide);
  range.value = String(currentSlide);
  previousButtons.forEach((button) => { button.disabled = currentSlide === 1; });
  nextButtons.forEach((button) => { button.disabled = currentSlide === totalSlides; });
  document.title = `${title} | OpenCast`;

  document.querySelectorAll(".thumbnail").forEach((button, index) => {
    button.classList.toggle("current", index + 1 === currentSlide);
  });

  if (updateHash) history.replaceState(null, "", `#slide=${currentSlide}`);
  preloadSlide(currentSlide + 1);
  preloadSlide(currentSlide - 1);
}

function goToSlide(number) {
  currentSlide = Math.min(totalSlides, Math.max(1, number));
  renderSlide();
}

function buildOverview() {
  const fragment = document.createDocumentFragment();
  slideTitles.forEach((title, index) => {
    const number = index + 1;
    const button = document.createElement("button");
    const image = document.createElement("img");
    const label = document.createElement("span");
    button.type = "button";
    button.className = "thumbnail";
    button.dataset.slide = String(number);
    button.setAttribute("aria-label", `Go to slide ${number}: ${title}`);
    image.loading = "lazy";
    image.src = `./slides/slide-${number}.png`;
    image.alt = "";
    label.textContent = `${number}. ${title}`;
    button.append(image, label);
    fragment.append(button);
  });
  thumbnailGrid.append(fragment);
}

function openOverview() {
  lastFocusedElement = document.activeElement;
  overview.hidden = false;
  document.body.style.overflow = "hidden";
  const current = thumbnailGrid.querySelector(`[data-slide="${currentSlide}"]`);
  current?.focus();
  current?.scrollIntoView({ block: "center" });
}

function closeOverview() {
  overview.hidden = true;
  document.body.style.overflow = "";
  lastFocusedElement?.focus();
}

previousButtons.forEach((button) => button.addEventListener("click", () => goToSlide(currentSlide - 1)));
nextButtons.forEach((button) => button.addEventListener("click", () => goToSlide(currentSlide + 1)));
range.addEventListener("input", () => goToSlide(Number(range.value)));
overviewButton.addEventListener("click", openOverview);
closeOverviewButton.addEventListener("click", closeOverview);

thumbnailGrid.addEventListener("click", (event) => {
  const button = event.target.closest(".thumbnail");
  if (!button) return;
  goToSlide(Number(button.dataset.slide));
  closeOverview();
});

overview.addEventListener("click", (event) => {
  if (event.target === overview) closeOverview();
});

fullscreenButton.addEventListener("click", async () => {
  if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
  else await document.exitFullscreen();
});

document.addEventListener("fullscreenchange", () => {
  const active = Boolean(document.fullscreenElement);
  fullscreenButton.setAttribute("aria-label", active ? "Exit fullscreen" : "Enter fullscreen");
});

document.addEventListener("keydown", (event) => {
  if (!overview.hidden) {
    if (event.key === "Escape") closeOverview();
    return;
  }
  if (["ArrowRight", "PageDown", " "].includes(event.key)) {
    event.preventDefault();
    goToSlide(currentSlide + 1);
  } else if (["ArrowLeft", "PageUp"].includes(event.key)) {
    event.preventDefault();
    goToSlide(currentSlide - 1);
  } else if (event.key === "Home") {
    goToSlide(1);
  } else if (event.key === "End") {
    goToSlide(totalSlides);
  } else if (event.key.toLowerCase() === "f") {
    fullscreenButton.click();
  } else if (event.key.toLowerCase() === "o") {
    openOverview();
  }
});

document.querySelector("#viewer").addEventListener("touchstart", (event) => {
  touchStartX = event.changedTouches[0].clientX;
}, { passive: true });

document.querySelector("#viewer").addEventListener("touchend", (event) => {
  if (touchStartX === null) return;
  const distance = event.changedTouches[0].clientX - touchStartX;
  if (Math.abs(distance) > 50) goToSlide(currentSlide + (distance < 0 ? 1 : -1));
  touchStartX = null;
}, { passive: true });

window.addEventListener("hashchange", () => {
  currentSlide = slideFromLocation();
  renderSlide(false);
});

buildOverview();
renderSlide(false);
