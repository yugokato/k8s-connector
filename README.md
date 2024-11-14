K8s Connector
======================
[![pre-commit.ci status](https://results.pre-commit.ci/badge/github/yugokato/k8s-connector/main.svg)](https://results.pre-commit.ci/latest/github/yugokato/k8s-connector/main)
[![Code style ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://docs.astral.sh/ruff/)


The "K8s Connector" allows you to programmatically access your Google Kubernetes Engine (GKE) cluster from your local environment. It leverages the [google/cloud-sdk](https://hub.docker.com/r/google/cloud-sdk/) docker image and provides a container class with a range of functions that utilize `kubectl` commands.


> [!NOTE]
> The library is mainly for accessing basic pods information, but not for performing any operations on resources to manage the cluster


## Use cases

- Collect pods status and logs upon receiving a 5xx response in automated API tests
- Check pods status as part of a test setup and skip the test if any pods are in `CrashLoopBackOff` status
- Check ongoing app deployment as a pre-hook for an API request and wait for it to complete before making the call
- Check pods status afrter a production release to ensure successful deployment


## Requirements

- Python 3.11+
- Docker and `google/cloud-sdk` image
- A working Google Kubernetes Engine (GKE) cluster
- Service Account file with sufficient read permissions


## Setup

1. Create a new virtual env
2. Run `pip install -e .`


## Usage examples

### Initialize the `K8sConnector` container

```pycon
>>> from k8s_connector import K8sConnector
>>> k8s = K8sConnector(service_account_file_path="/path/key.json", cluster_name="my-cluster", zone="us-west1-a")
2024-01-01T00:00:00.000-0000 - Starting a container google/cloud-sdk:latest
2024-01-01T00:00:00.208-0000 - Started: c9b66e7152bbebe86445aaa9c6de4a2ee55b0ec8c48aa0b17a10d1ba8fcab7b6
2024-01-01T00:00:00.213-0000 - Activating Service Account credentials...
2024-01-01T00:00:00.213-0000 - Executing command: sh -c 'gcloud auth activate-service-account --key-file=/tmp/key.json --project myproject'
2024-01-01T00:00:01.133-0000 - output:
Activated service account credentials for: [sa@myproject.iam.gserviceaccount.com]

2024-01-01T00:00:01.158-0000 - Fetching credentials for the cluster...
2024-01-01T00:00:01.158-0000 - Executing command: sh -c 'gcloud container clusters get-credentials my-cluster --zone us-west1-a'
2024-01-01T00:00:02.660-0000 - output:
Fetching cluster endpoint and auth data.
kubeconfig entry generated for my-cluster.
```

### Execute a raw command using `exec_run()`

```pycon
>>> exit_code, output = k8s.exec_run('kubectl version --client')
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl version --client'
2024-01-01T00:00:00.304-0000 - output:
Client Version: v1.28.12-dispatcher
Kustomize Version: v5.0.4-0.20230601165947-6ce0bf390ce3
```

### Get pods information in all namespaces

```pycon
# Get pods
>>> pods = k8s.get_pods()
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl get pods -A'
2024-01-01T00:00:00.879-0000 - output:
NAMESPACE   NAME                    READY   STATUS             RESTARTS         AGE
example     app1-c44949f4ba-zsxpd   3/3     Running            0                30m
example     app1-c44949f4ba-gjepp   3/3     Running            0                31m
example     app2-685dbfc75d-fzcmr   3/3     Running            0                31m
example     app2-685dbfc75d-pxhmj   3/3     Running            0                29m
...

# Get namespaces
>>> namespaces = k8s.get_namespaces()
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl get namespaces'
2024-01-01T00:00:00.279-0000 - output:
NAME                              STATUS   AGE
example                           Active   30d
...
```

### Get pods information for a specific application

```pycon
# Get pods
>>> pods = k8s.app1.get_pods()
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl get pods -l app=app1 -n example'
2024-01-01T00:00:00.625-0000 - output:
NAME                    READY   STATUS    RESTARTS   AGE
app1-c44949f4ba-zsxpd   3/3     Running   0          3d5h
app1-c44949f4ba-gjepp   3/3     Running   0          3d5h
...

# Get pods logs
>>> logs = k8s.app1.get_logs()
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl logs -l app=app1 -n example --since=30s --tail=-1'
2024-01-01T00:00:01.342-0000 - output:
[2024-01-01T00:00:00.000000+00:00] [INFO]{fb0f6a7a-66e7-4411-802b-e9b501f196f7}[app1.api.foo] - GET /foo
[2024-01-01T00:00:00.000000+00:00] [INFO]{941e391a-3b0f-464b-a35b-979c1f084443}[app1.api.bar] - GET /bar
...

# Stream pods logs (with filtering)
>> k8s.app1.get_logs(follow=True, grep="ERROR|CRITICAL")
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl logs -l app=app1 -n example --since=30s --tail=-1 -f --max-log-requests=30 | GREP_COLOR="1;32" stdbuf -o0 grep -E -E "ERROR|CRITICAL" --color=always'
[2024-01-01T00:00:00.000000+00:00] [ERROR]{b9e75fd4-7f82-446e-92fd-48f23a81dbb4}[app1.api.foo] - foo
[2024-01-01T00:00:00.000000+00:00] [ERROR]{7e6f3618-2ec5-4fe5-ae93-4aaa387f3263}[app1.api.bar] - bar
...

# Get environment variables set as a configmap
>>> configmap_data = k8s.app1.get_configmap_data()
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl get configmap -l app=app1 -n example -o json | jq '"'"'.items[] | select(.kind == "ConfigMap") | .data'"'"''
2024-01-01T00:00:00.609-0000 - output:
{
  "APP1_ENV_VAR1": "true",
  "APP1_ENV_VAR2": "false",
  ...
}

# Get metrics
>>> output = k8s.app1.top()
2024-01-01T00:00:00.000-0000 - Executing command: sh -c 'kubectl top pod --containers --sort-by=cpu -l app=app1 -n example'
2024-01-01T00:00:00.590-0000 - output:
POD                     NAME          CPU(cores)   MEMORY(bytes)   
app1-c44949f4ba-zsxpd   app1           617m         1253Mi                       
app1-c44949f4ba-gjepp   app1           607m         1174Mi      
...     

# Wait for app deployment to complete after a successful build
>>> k8s.app1.wait_for_deployment()
2024-01-01T01:00:00.000-0000 - Waiting for app1 deployment to start...
2024-01-01T01:01:57.730-0000 - app1 deployment started
2024-01-01T01:01:57.730-0000 - Waiting for deployment of app1 to complete...
2024-01-01T01:01:57.730-0000 - Executing command: sh -c 'kubectl rollout status deployment/app1 -n example --timeout=360s'
2024-01-01T01:04:22.022-0000 - output:
Waiting for deployment "app1" rollout to finish: 2 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 2 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 2 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 2 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 3 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 3 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 3 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 3 out of 4 new replicas have been updated...
Waiting for deployment "app1" rollout to finish: 1 old replicas are pending termination...
Waiting for deployment "app1" rollout to finish: 1 old replicas are pending termination...
Waiting for deployment "app1" rollout to finish: 1 old replicas are pending termination...
Waiting for deployment "app1" rollout to finish: 3 of 4 updated replicas are available...
deployment "app1" successfully rolled out

2024-01-01T01:04:22.022-0000 - Executing command: sh -c 'kubectl get pods -l app=app1 -n example'
2024-01-01T01:09:22.657-0000 - output:
NAME                    READY   STATUS    RESTARTS      AGE
app1-7d575bb744-7c2fg   3/3     Running   0             91s
app1-7d575bb744-9jxtc   3/3     Running   0             2m37s
app1-7d575bb744-fzcmr   3/3     Running   0             2m38s
app1-7d575bb744-pxhmj   3/3     Running   0             37s

2024-01-01T01:04:22.657-0000 - Deployment of app1 completed (took 144.926855802536 seconds)
```

> [!TIP]
> Most functions return the raw output by default, but some support `parse` parameter which will parse the output and return it as an object (eg. Returns the tabulated output as a list of dictionaries)
