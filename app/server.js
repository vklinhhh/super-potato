const express = require("express");
const multer = require("multer");
const { spawn } = require("node:child_process");
const { mkdtemp, rm } = require("node:fs/promises");
const os = require("node:os");
const path = require("node:path");

const app = express();
const port = Number(process.env.PORT || 3000);
const projectRoot = path.resolve(__dirname, "..");
const python = path.join(projectRoot, ".venv", "bin", "python");
const upload = multer({ storage: multer.memoryStorage(), limits: { fileSize: 20 * 1024 * 1024 } });

app.use(express.static(path.join(__dirname, "public")));
app.use(express.json());

function runPipeline(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, args, { cwd: projectRoot, env: process.env });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code !== 0) {
        reject(new Error(stderr.trim() || `Pipeline exited with code ${code}.`));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch {
        reject(new Error(`Pipeline returned invalid JSON: ${stdout}`));
      }
    });
  });
}

function runCommand(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, args, { cwd: projectRoot, env: process.env });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", reject);
    child.on("close", (code) => {
      if (code === 0) resolve();
      else reject(new Error(stderr.trim() || `Command exited with code ${code}.`));
    });
  });
}

app.post("/api/reset-redis", async (req, res) => {
  if (req.body?.confirmation !== "RESET DEV REDIS") {
    res.status(400).json({ error: 'Type "RESET DEV REDIS" to confirm this destructive action.' });
    return;
  }
  try {
    await runCommand(["scripts/reset_redis.py", "--env", "dev", "--yes"]);
    res.json({ message: "Dev Redis was reset." });
  } catch (error) {
    res.status(502).json({ error: error.message });
  }
});

app.post(
  "/api/lookup",
  upload.fields([{ name: "overview", maxCount: 1 }, { name: "plateImage", maxCount: 1 }]),
  async (req, res) => {
    const { plateNumber, plateState } = req.body || {};
    const overview = req.files?.overview?.[0];
    const plateImage = req.files?.plateImage?.[0];
    if (!plateNumber?.trim() || !plateState?.trim() || !overview || !plateImage) {
      res.status(400).json({ error: "Plate number, plate state, overview image, and plate image are required." });
      return;
    }

    const requestDirectory = await mkdtemp(path.join(os.tmpdir(), "vbio-"));
    try {
      const overviewPath = path.join(requestDirectory, `overview${path.extname(overview.originalname) || ".jpg"}`);
      const platePath = path.join(requestDirectory, `plate${path.extname(plateImage.originalname) || ".jpg"}`);
      await Promise.all([
        require("node:fs/promises").writeFile(overviewPath, overview.buffer),
        require("node:fs/promises").writeFile(platePath, plateImage.buffer),
      ]);
      const result = await runPipeline([
        "-m", "vbio.vehicle_lookup_service", overviewPath,
        "--plate-number", plateNumber.trim().toUpperCase(),
        "--plate-state", plateState.trim().toUpperCase(),
        "--plate-image", platePath,
        "--use-redis",
        "--redis-only",
      ]);
      res.json(result);
    } catch (error) {
      res.status(502).json({ error: error.message });
    } finally {
      await rm(requestDirectory, { recursive: true, force: true });
    }
  },
);

app.listen(port, () => console.log(`VBIo local app: http://localhost:${port}`));
