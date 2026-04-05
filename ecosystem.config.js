module.exports = {
    apps: [
        {
            name: "agent-studio-v2",
            script: ".venv/bin/uvicorn",
            args: "app.main:app --port 5000",
            interpreter: "none",
            env: {
                NODE_ENV: "production",
            }
        }
    ]
};
