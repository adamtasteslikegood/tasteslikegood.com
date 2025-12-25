(function(){
    const form = document.getElementById('pantry-form');
    const logEl = document.getElementById('pantry-log');
    const recipeEl = document.getElementById('pantry-recipe');
    const saveBtn = document.getElementById('save-cookbook');
    let currentRun = null;

    function appendLog(obj){
        logEl.textContent += JSON.stringify(obj, null, 2) + "\n";
    }

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        logEl.textContent = '';
        recipeEl.textContent = '';
        saveBtn.disabled = true;

        const payload = {
            pantry: document.getElementById('pantry-text').value,
            constraints: document.getElementById('constraints').value,
        };
        const res = await fetch('/api/pantry/generate', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        currentRun = data.run_id;
        const es = new EventSource(`/api/pantry/stream/${currentRun}`);
        es.onmessage = (ev) => {
            const msg = JSON.parse(ev.data);
            appendLog(msg);
            if (msg.type === 'final'){
                recipeEl.textContent = JSON.stringify(msg.recipe, null, 2);
                saveBtn.disabled = false;
                es.close();
            }
        };
        es.onerror = () => { appendLog({type:'error', message:'Stream error'}); es.close(); };
    });

    saveBtn.addEventListener('click', async () => {
        if (!recipeEl.textContent) return;
        const recipe = JSON.parse(recipeEl.textContent);
        const resp = await fetch('/api/cookbook/recipes', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({recipe})
        });
        const result = await resp.json();
        appendLog({type:'save', result});
    });
})();
