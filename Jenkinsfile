pipeline {
    agent {
        kubernetes {
            yamlFile 'K8sPod.yaml'
        }
    }

    environment {
        DINGTALK_URL = "${DINGTALK_WEBHOOK_URL}"
        DOCKER_REPO = "example/viktor"
        OWNER_PHONE = "${BUILD_OWNER_PHONE}"
    }

    options {
        disableConcurrentBuilds()
    }


    stages {
        stage('prepare') {
            steps {
                sh '''
                  curl -d '{"msgtype": "text","text": {"content":"'"$JOB_NAME"': 开始构建镜像'"$BUILD_URL"'"}}' -H 'Content-Type: application/json'  "$DINGTALK_URL"
                '''
                script {
                    env.PRO_IMAGE_TAG = ""
                    env.IMAGE_TAG = ""
                    env.ENV_NAME = ""
                }
            }
        }

        stage('build-Parallel') {

            when {
                anyOf {
                    branch 'dev'
                    branch 'release'
                    branch 'master'
                }
            }
            failFast false

            parallel {

                stage('build-test') {
                    when {
                        beforeAgent true
                        branch 'dev'
                    }

                    steps {
                        container('docker') {
                            echo "build-test"
                            script {
                                env.ENV_NAME = "test"
                            }
                        }
                    }
                }

                stage('build-uat') {

                    when {
                        beforeAgent true
                        branch 'release'
                    }

                    steps {
                        container('docker') {
                            echo "build-uat"
                            script {
                                env.ENV_NAME = "uat"
                            }
                        }
                    }
                }

                stage('build-prod') {

                    when {
                        beforeAgent true
                        branch 'master'
                    }

                    steps {
                        container('docker') {
                            echo "build-prod"
                            script {
                                env.ENV_NAME = "prod"
                            }
                        }
                    }
                }
            }

        }

        stage("artifacts-manage"){
            steps {
                // sh '''
                //     curl -d '{"msgtype": "text","text": {"content":"'"$JOB_NAME"': 应用打包成功，开始构建镜像..."}}' -H 'Content-Type: application/json' "$DINGTALK_URL"
                // '''
                container('docker') {
                    echo "artifacts"
                    script {
                        // 先获取短 commit ID
                        def shortCommit = sh(script: 'echo ${GIT_COMMIT:0:8}', returnStdout: true).trim()
                        def timestamp = sh(script: 'date "+%Y%m%d%H%M%S"', returnStdout: true).trim()
                        
                        env.IMAGE_TAG = "${ALI_DOCKER_HUB_HOST}/${DOCKER_REPO}:${GIT_BRANCH}-${shortCommit}-${timestamp}"
                        env.PRO_IMAGE_TAG = "${ALI_DOCKER_HUB_PRO_HOST}/${DOCKER_REPO}:${GIT_BRANCH}-${shortCommit}-${timestamp}"
                        
                        echo "镜像标签: ${IMAGE_TAG}"
                        echo "企业版镜像标签: ${PRO_IMAGE_TAG}"
                    }
                    sh '''
                        rm -f Dockerfile
                        cp "conf.$ENV_NAME.Dockerfile" Dockerfile
                        rm -f *.Dockerfile
                        cat Dockerfile
                        docker login ${ALI_DOCKER_HUB_HOST} -u ${ALI_DOCKER_HUB_USER} -p ${ALI_DOCKER_HUB_PWD} || exit 1
                        docker build -t ${IMAGE_TAG}  . || exit 1
                        docker push ${IMAGE_TAG} || exit 1
                        docker login ${ALI_DOCKER_HUB_PRO_HOST} -u ${ALI_DOCKER_HUB_PRO_USER} -p ${ALI_DOCKER_HUB_PRO_PWD} || exit 1
                        docker tag ${IMAGE_TAG} ${PRO_IMAGE_TAG}
                        docker push ${PRO_IMAGE_TAG} || exit 1
                    '''
                }
                sh '''
                    curl -d '{"msgtype": "text","text": {"content":"'"$JOB_NAME"': 镜像构建成功'"$IMAGE_TAG"'，已推送至镜像仓库"}, "at": { "atMobiles": ["'"$OWNER_PHONE"'"] }}' -H 'Content-Type: application/json' "$DINGTALK_URL"
                '''
                // 推完镜像后，自动触发yaml文件更新
                sh '''
                    curl -d '{ "appImage": "'"$PRO_IMAGE_TAG"'" }' -H 'Content-Type: application/json' "$UPDATE_CD_URL"
                '''
            }
        }

    }

    post {

        changed{
            echo 'I changed!'
        }

        failure{
            echo 'I failed!'
            sh '''
              curl -d '{"msgtype": "text","text": {"content":"'"$JOB_NAME"': 打包/构建失败"}, "at": { "atMobiles": ["'"$OWNER_PHONE"'"] }}' -H 'Content-Type: application/json' "$DINGTALK_URL"
            '''
        }

        success{
            echo 'I success'
        }

        always{
            echo 'I always'
         }

        unstable{
            echo "unstable"
        }
        aborted{
            echo "aborted"
        }
    }

}