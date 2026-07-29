pipeline {
    agent { label 'docker_node' }

    options {
        timestamps()
        disableConcurrentBuilds()
    }

    environment {
        COMPOSE_PROJECT_NAME = 'hsv-invoice-extract-lite'
    }

    stages {
        stage('Install deps & test') {
            steps {
                sh '''
                    set -eu
                    command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
                    export PATH="$HOME/.local/bin:$PATH"
                    mkdir -p reports
                    uv sync --frozen --group dev
                    uv run ruff check .
                    uv run ruff format --check .
                    uv run pytest --junitxml=reports/pytest.xml
                '''
            }
            post {
                always {
                    junit 'reports/pytest.xml'
                }
            }
        }

        stage('Build image') {
            steps {
                sh 'docker compose build'
            }
        }

        stage('Deploy') {
            steps {
                sh '''
                    set -eu
                    if [ ! -f .env ]; then
                        echo "ERROR: .env not found on deploy host. This pipeline never creates or edits it -- an operator must place it here manually before the first deploy. See docs/OPERATIONS.md." >&2
                        exit 1
                    fi
                    mkdir -p data && chown -R 1000:1000 data
                    docker compose up -d
                '''
            }
        }

        stage('Smoke test') {
            steps {
                sh '''
                    set -eu
                    for i in $(seq 1 10); do
                        if curl -fsS "http://localhost:8000/healthz"; then
                            echo "Service is healthy."
                            exit 0
                        fi
                        echo "Waiting for service to become healthy... ($i/10)"
                        sleep 3
                    done
                    echo "Service failed health check after deploy." >&2
                    docker compose logs --tail=100
                    exit 1
                '''
            }
        }
    }

    post {
        always {
            cleanWs()
        }
    }
}
