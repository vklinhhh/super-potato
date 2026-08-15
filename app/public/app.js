const form = document.querySelector("#lookup-form");
const status = document.querySelector("#status");
const result = document.querySelector("#result");
const summary = document.querySelector("#summary");
const candidates = document.querySelector("#candidates");
const raw = document.querySelector("#raw");
const visualisation = document.querySelector("#visualisation");
const overviewCanvas = document.querySelector("#overview-canvas");
const platePreview = document.querySelector("#plate-preview");

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const image = new Image();
    image.onload = () => { URL.revokeObjectURL(url); resolve(image); };
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Could not render the submitted image.")); };
    image.src = url;
  });
}

async function renderInputs(match) {
  const overview = form.elements.overview.files[0];
  const plateImage = form.elements.plateImage.files[0];
  if (!overview || !plateImage) return;
  const [overviewImage, cropImage] = await Promise.all([loadImage(overview), loadImage(plateImage)]);
  overviewCanvas.width = overviewImage.naturalWidth;
  overviewCanvas.height = overviewImage.naturalHeight;
  const context = overviewCanvas.getContext("2d");
  context.drawImage(overviewImage, 0, 0);
  const drawBoxes = match?.phase !== "whole_image_fallback";
  if (drawBoxes) {
    for (const vehicle of match?.vehicles || []) {
      if (!vehicle.box) continue;
      const [x1, y1, x2, y2] = vehicle.box;
      const isWinner = vehicle.index === match.vehicle_id;
      context.strokeStyle = isWinner ? "#22a447" : "#d73a49";
      context.lineWidth = Math.max(2, overviewImage.naturalWidth / 500);
      context.strokeRect(x1 * overviewImage.naturalWidth, y1 * overviewImage.naturalHeight,
        (x2 - x1) * overviewImage.naturalWidth, (y2 - y1) * overviewImage.naturalHeight);
      context.fillStyle = context.strokeStyle;
      context.font = `${Math.max(14, overviewImage.naturalWidth / 45)}px sans-serif`;
      context.fillText(`${isWinner ? "MATCH " : ""}${vehicle.label}`, x1 * overviewImage.naturalWidth + 4,
        Math.max(18, y1 * overviewImage.naturalHeight - 5));
    }
  }
  const plateUrl = URL.createObjectURL(plateImage);
  platePreview.onload = () => URL.revokeObjectURL(plateUrl);
  platePreview.src = plateUrl;
  visualisation.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  status.textContent = "Calling MCAP, matching the plate, and scoring Redis evidence…";
  result.hidden = true;
  try {
    const response = await fetch("/api/lookup", { method: "POST", body: new FormData(form) });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || "Lookup failed.");
    const enrichment = data.enrichment || {};
    summary.innerHTML = [
      ["Plate", `${data.key.plate} / ${data.key.plateState}`],
      ["Matched vehicle", data.mcap?.makeModel || "Unknown"],
      ["Match phase", data.plate_match?.phase || "No match"],
      ["Tier", enrichment.tier || "NONE"],
      ["Result", `${enrichment.make || "Unknown"} ${enrichment.model || ""}`.trim()],
      ["Confidence", enrichment.confidence ?? "n/a"],
    ].map(([term, value]) => `<dt>${term}</dt><dd>${value}</dd>`).join("");
    candidates.innerHTML = (enrichment.candidates || []).map((candidate) =>
      `<tr><td>${candidate.make}</td><td>${candidate.model}</td><td>${candidate.score}</td><td>${candidate.posterior}</td><td>${candidate.observationCount}</td></tr>`
    ).join("");
    raw.textContent = JSON.stringify(data, null, 2);
    await renderInputs(data.plate_match);
    result.hidden = false;
    status.textContent = "Lookup complete.";
  } catch (error) {
    status.textContent = error.message;
  }
});

document.querySelector("#reset-redis").addEventListener("click", async () => {
  const confirmation = document.querySelector("#reset-confirmation").value;
  if (confirmation !== "RESET DEV REDIS") {
    status.textContent = 'Type "RESET DEV REDIS" exactly before resetting.';
    return;
  }
  try {
    const response = await fetch("/api/reset-redis", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmation }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Reset failed.");
    result.hidden = true;
    document.querySelector("#reset-confirmation").value = "";
    status.textContent = data.message;
  } catch (error) {
    status.textContent = error.message;
  }
});
