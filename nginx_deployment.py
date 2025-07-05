import time
from fastapi import FastAPI, Header, HTTPException, Query
import kubernetes.client
import kubernetes.config
from kubernetes.client.rest import ApiException
import subprocess
import logging
import os
import yaml
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -------------------- Constants --------------------
API_KEY = "deploy-nginx"
TEMP_YAML_FILE = "temp_resource.yaml"
RETRY_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 5
COMMAND_TIMEOUT_SECONDS = 15

# -------------------- Kubernetes Config --------------------
try:
    kubernetes.config.load_kube_config()
except kubernetes.config.ConfigException:
    kubernetes.config.load_incluster_config()

core_v1 = kubernetes.client.CoreV1Api()
apps_v1 = kubernetes.client.AppsV1Api()

# -------------------- Utility: Run Shell Command --------------------
@retry(stop=stop_after_attempt(RETRY_ATTEMPTS), wait=wait_fixed(RETRY_WAIT_SECONDS), retry=retry_if_exception_type(Exception))
def run_command(command: list) -> tuple:
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
        return output.decode().strip(), error.decode().strip()
    except subprocess.TimeoutExpired:
        raise Exception(f"Command timed out after {COMMAND_TIMEOUT_SECONDS} seconds")


# -------------------- API Key Validator --------------------
def verify_api_key(api_key: str):
    if api_key != API_KEY:
        logger.warning("Unauthorized API request attempt.")
        raise HTTPException(status_code=401, detail="Invalid API key")

# -------------------- Kubernetes: Check Namespace Existence --------------------
def namespace_exists(namespace: str) -> bool:
    """Return True if namespace exists, else False."""
    v1 = kubernetes.client.CoreV1Api()
    try:
        v1.read_namespace(namespace)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        logger.error(f"Failed to check namespace: {e}")
        raise

# -------------------- Kubernetes: Apply Resource from YAML --------------------
def apply_kubernetes_resource(resource: dict) -> str:
    """Write dict to YAML and apply it using kubectl."""
    try:
        with open(TEMP_YAML_FILE, "w") as f:
            yaml.safe_dump(resource, f)

        cmd = ["kubectl", "apply", "-f", TEMP_YAML_FILE]
        output, error = run_command(cmd)

        os.remove(TEMP_YAML_FILE)

        if "Error" in error or "error" in output.lower():
            raise Exception(f"kubectl apply failed: {error or output}")

        return output
    except Exception as e:
        logger.error(f"Failed to apply Kubernetes resource: {e}")
        raise

# -------------------- Create Namespace Conditionally --------------------
def create_namespace(namespace: str) -> dict:
    if namespace_exists(namespace):
        logger.info(f"Namespace '{namespace}' already exists.")
        return {
            "status": "exists",
            "namespace": namespace,
            "message": "Namespace already exists. No action taken."
        }

    # Define the namespace manifest
    resource = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": namespace}
    }

    try:
        output = apply_kubernetes_resource(resource)
        logger.info(f"Namespace '{namespace}' created successfully.")
        return {
            "status": "created",
            "namespace": namespace,
            "output": output,
            "message": f"Namespace '{namespace}' created via YAML"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------------------- Deployment Check/Create --------------------
def deployment_exists(namespace: str, name: str) -> bool:
    try:
        apps_v1.read_namespaced_deployment(name, namespace)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise

def deploy_nginx(namespace: str) -> dict:
    if deployment_exists(namespace, "nginx-deployment"):
        return {"status": "exists", "namespace": namespace, "deployment": "nginx-deployment"}
    # Define NGINX deployment manifest as a Python dictionary
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "nginx-deployment",
            "namespace": namespace,  # Apply in correct namespace
            "labels": {
                "app": "nginx"
            }
        },
        "spec": {
            "replicas": 1,
            "selector": {
                "matchLabels": {
                    "app": "nginx"
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": "nginx"
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "name": "nginx",
                            "image": "nginx:latest",
                            "ports": [
                                {"containerPort": 80}
                            ]
                        }
                    ]
                }
            }
        }
    }

    try:
        output = apply_kubernetes_resource(deployment)
        logger.info(f"Deployment 'nginx-deployment' applied in namespace '{namespace}'.")
        return {
            "status": "success",
            "namespace": namespace,
            "output": output,
            "message": "NGINX deployment applied successfully."
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

# -------------------- Service Check/Create --------------------
def service_exists(namespace: str, name: str) -> bool:
    try:
        core_v1.read_namespaced_service(name, namespace)
        return True
    except ApiException as e:
        if e.status == 404:
            return False
        raise

def create_nginx_service(namespace: str) -> dict:
    if service_exists(namespace, "nginx-service"):
        return {"status": "exists", "namespace": namespace, "service": "nginx-service"}
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "nginx-service",
            "namespace": namespace
        },
        "spec": {
            "selector": {
                "app": "nginx"
            },
            "ports": [
                {
                    "protocol": "TCP",
                    "port": 81,
                    "targetPort": 80
                }
            ],
            "type": "ClusterIP"  # Change to NodePort or LoadBalancer as needed
        }
    }
    output = apply_kubernetes_resource(service)
    return {"status": "service-created", "namespace": namespace, "output": output}


# -------------------- FastAPI App --------------------
app = FastAPI()

@app.post("/k8s/deploy")
def deploy_namespace_and_nginx(
    api_key: str = Header(..., alias="x-api-key"),
    namespace: str = Query("nginx")
):
    verify_api_key(api_key)

    try:
        ns_result = create_namespace(namespace)
        deploy_result = deploy_nginx(namespace)
        service_result = create_nginx_service(namespace)

        return {
            "namespace_result": ns_result,
            "deployment_result": deploy_result,
            "service_result": service_result,
            "message": "Namespace, NGINX Deployment, and Service executed successfully."
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# curl -X POST "http://localhost:4000/k8s/deploy?namespace=nginx" -H "x-api-key: deploy-nginx"
# uvicorn :app --reload --port 4000