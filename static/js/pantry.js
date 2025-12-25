(() => {
  const form = document.getElementById("pantry-form");
  const logEl = document.getElementById("progress-log");
  const recipeCard = document.getElementById("recipe-card");
  const recipeJson = document.getElementById("recipe-json");
  const saveBtn = document.getElementById("save-recipe-btn");
  let currentRun = null;
  let session = null;

  function appendLog(obj) {
    logEl.textContent += JSON.stringify(obj, null, 2) + "\n";
  }

  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    logEl.textContent = "";
    recipeCard.style.display = "none";
    const pantry = document.getElementById("pantry-text").value;
    const constraints = document.getElementById("constraints").value;
    const res = await fetch("/api/pantry/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pantry, constraints }),
    });
    const payload = await res.json();
    currentRun = payload.run_id;
    session = payload.session_id;
    const es = new EventSource(`/api/pantry/stream/${currentRun}`);
    es.onmessage = (ev) => {
      const data = JSON.parse(ev.data);
      appendLog(data);
      if (data.type === "final" && data.recipe) {
        recipeJson.textContent = JSON.stringify(data.recipe, null, 2);
        recipeCard.style.display = "block";
        saveBtn.onclick = () => saveRecipe(data.recipe);
        es.close();
      }
    };
  });

  async function saveRecipe(recipe) {
    const res = await fetch("/api/cookbook/recipes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recipe }),
    });
    const payload = await res.json();
    appendLog({ saved: payload.id });
  }
})();
