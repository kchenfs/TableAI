# Use the official AWS base image for the Python 3.13 Lambda runtime.
FROM public.ecr.aws/lambda/python:3.13

# Set a working directory inside the container.
WORKDIR ${LAMBDA_TASK_ROOT}

# Copy the requirements file into the container.
COPY requirements.txt .

# Install the dependencies from requirements.txt.
RUN pip install -r requirements.txt

# Copy your application code (app.py) into the working directory.
COPY app.py .

# --- ADD THIS PART ---
# Copy the RAG data files into the image's working directory.
COPY rag_index.faiss .
COPY rag_chunks.json .
# ---------------------

# Set the CMD to your handler.
# Format: CMD [ "<filename>.<handler_function_name>" ]
CMD [ "app.lambda_handler" ]