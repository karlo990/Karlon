FROM python:3.12-slim

WORKDIR /app

# llama-cpp-python comes as a prebuilt CPU wheel from this index (no
# compiling); it bundles its own OpenMP and needs only the C++ runtime.
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 libgomp1 \
    && rm -rf /var/lib/apt/lists/*
ENV PIP_EXTRA_INDEX_URL=https://abetlen.github.io/llama-cpp-python/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Davis's local model, baked into the image so it's there at start (this
# layer is cached between pushes until the model settings change). Change
# DAVIS_LOCAL_MODEL_REPO / DAVIS_LOCAL_MODEL_FILE here and in the Space's
# variables together.
ENV DAVIS_LOCAL_MODEL_REPO=Qwen/Qwen2.5-1.5B-Instruct-GGUF \
    DAVIS_LOCAL_MODEL_FILE=qwen2.5-1.5b-instruct-q4_k_m.gguf \
    DAVIS_LOCAL_MODEL_DIR=/models
RUN python -c "import os; from huggingface_hub import hf_hub_download; \
hf_hub_download(os.environ['DAVIS_LOCAL_MODEL_REPO'], os.environ['DAVIS_LOCAL_MODEL_FILE'], local_dir='/models')" \
    && rm -rf /models/.cache

COPY . .

# Hugging Face Spaces routes traffic to port 7860 by default
ENV PORT=7860
EXPOSE 7860

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
