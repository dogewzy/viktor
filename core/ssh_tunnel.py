"""
SSH 隧道管理模块。

用于通过 SSH 跳板机连接内网数据库。
支持 OpenSSH 新格式私钥。
"""
import contextlib
import socket
from pathlib import Path
from typing import Optional

from loguru import logger


def _load_private_key(pkey_path: str):
    """加载私钥，支持多种格式。"""
    from paramiko import RSAKey, Ed25519Key, ECDSAKey
    import paramiko

    path = Path(pkey_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"SSH 私钥不存在: {path}")

    key_content = path.read_text()

    # 尝试不同的密钥类型
    key_classes = [RSAKey, Ed25519Key, ECDSAKey]
    
    for key_class in key_classes:
        try:
            key = key_class.from_private_key_file(str(path))
            logger.debug(f"成功加载 {key_class.__name__} 格式的私钥")
            return key
        except Exception:
            continue

    # 如果是 OpenSSH 新格式，尝试用密码加载
    for key_class in key_classes:
        try:
            key = key_class.from_private_key_file(str(path), password=None)
            return key
        except Exception:
            continue

    raise ValueError(f"无法加载私钥 {path}，请确保是 RSA/Ed25519/ECDSA 格式")


class SSHTunnelManager:
    """
    SSH 隧道管理器。

    支持为不同项目的数据库连接器创建独立的 SSH 隧道。
    """

    # SSH 跳板机配置
    DEFAULT_JUMP_HOST = ""
    DEFAULT_JUMP_PORT = 20140
    DEFAULT_USERNAME = "viewer"
    DEFAULT_PRIVATE_KEY = "~/.ssh/id_vb1"

    def __init__(
        self,
        remote_host: str,
        remote_port: int,
        local_port: Optional[int] = None,
        jump_host: Optional[str] = None,
        jump_port: Optional[int] = None,
        username: Optional[str] = None,
        private_key: Optional[str] = None,
    ):
        """
        初始化隧道管理器。

        Args:
            remote_host: 目标数据库内网地址
            remote_port: 目标数据库端口
            local_port: 本地转发端口（默认自动分配）
            jump_host: SSH 跳板机地址
            jump_port: SSH 跳板机端口
            username: SSH 用户名
            private_key: SSH 私钥路径
        """
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.local_port = local_port or 0  # 0 表示自动分配
        
        self.jump_host = jump_host or self.DEFAULT_JUMP_HOST
        self.jump_port = jump_port or self.DEFAULT_JUMP_PORT
        self.username = username or self.DEFAULT_USERNAME
        self.private_key = private_key or self.DEFAULT_PRIVATE_KEY
        
        self._ssh_client = None
        self._local_socket = None
        self._actual_local_port: Optional[int] = None

    def _find_free_port(self) -> int:
        """找一个空闲端口。"""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            s.listen(1)
            port = s.getsockname()[1]
        return port

    def start(self) -> int:
        """
        启动 SSH 隧道。

        Returns:
            本地端口号
        """
        import paramiko
        import threading
        import select

        private_key_path = Path(self.private_key).expanduser()
        if not private_key_path.exists():
            raise FileNotFoundError(f"SSH 私钥不存在: {private_key_path}")

        # 加载私钥
        pkey = _load_private_key(str(private_key_path))

        # 建立 SSH 连接
        self._ssh_client = paramiko.SSHClient()
        self._ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        logger.info(
            "正在连接 SSH 跳板机 {}:{} ...",
            self.jump_host, self.jump_port
        )
        
        self._ssh_client.connect(
            hostname=self.jump_host,
            port=self.jump_port,
            username=self.username,
            pkey=pkey,
            timeout=30,
        )

        # 找到本地监听端口
        if self.local_port == 0:
            self._actual_local_port = self._find_free_port()
        else:
            self._actual_local_port = self.local_port

        # 创建本地监听 socket
        self._local_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._local_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._local_socket.bind(('127.0.0.1', self._actual_local_port))
        self._local_socket.listen(5)

        logger.info(
            "SSH 隧道已建立: {}:{} -> {}:{} (本地端口: {})",
            self.jump_host, self.jump_port,
            self.remote_host, self.remote_port,
            self._actual_local_port
        )

        # 启动转发线程
        def forward():
            while True:
                try:
                    client, addr = self._local_socket.accept()
                    # 为每个连接创建转发通道
                    channel = self._ssh_client.get_transport().open_channel(
                        'direct-tcpip',
                        (self.remote_host, self.remote_port),
                        addr
                    )
                    
                    def relay(client_sock, server_chan):
                        try:
                            while True:
                                readable, _, _ = select.select([client_sock, server_chan], [], [], 1)
                                if client_sock in readable:
                                    data = client_sock.recv(1024)
                                    if not data:
                                        break
                                    server_chan.send(data)
                                if server_chan in readable:
                                    data = server_chan.recv(1024)
                                    if not data:
                                        break
                                    client_sock.send(data)
                        except Exception:
                            pass
                        finally:
                            client_sock.close()
                            server_chan.close()
                    
                    # 双向转发
                    t1 = threading.Thread(target=relay, args=(client, channel))
                    t2 = threading.Thread(target=relay, args=(channel, client))
                    t1.daemon = True
                    t2.daemon = True
                    t1.start()
                    t2.start()
                    
                except Exception as e:
                    if self._local_socket:
                        logger.debug("转发循环结束: {}", e)
                    break

        self._forward_thread = threading.Thread(target=forward)
        self._forward_thread.daemon = True
        self._forward_thread.start()

        return self._actual_local_port

    def stop(self) -> None:
        """停止 SSH 隧道。"""
        if self._local_socket:
            try:
                self._local_socket.close()
            except Exception:
                pass
            self._local_socket = None

        if self._ssh_client:
            try:
                self._ssh_client.close()
            except Exception:
                pass
            self._ssh_client = None

        logger.info("SSH 隧道已关闭: {}:{} -> {}:{}",
                   self.jump_host, self.jump_port,
                   self.remote_host, self.remote_port)

    @property
    def is_active(self) -> bool:
        """检查隧道是否活跃。"""
        return (
            self._ssh_client is not None and 
            self._ssh_client.get_transport() is not None and
            self._ssh_client.get_transport().is_active()
        )


@contextlib.contextmanager
def tunnel_context(
    remote_host: str, 
    remote_port: int, 
    local_port: Optional[int] = None,
    jump_host: Optional[str] = None,
    jump_port: Optional[int] = None,
    username: Optional[str] = None,
    private_key: Optional[str] = None,
):
    """
    SSH 隧道上下文管理器。

    用法:
        with tunnel_context('db.internal.com', 3306) as local_port:
            # 使用 127.0.0.1:local_port 连接数据库
            ...

    Args:
        remote_host: 目标数据库内网地址
        remote_port: 目标数据库端口
        local_port: 本地转发端口（默认自动分配）
        jump_host: SSH 跳板机地址
        jump_port: SSH 跳板机端口
        username: SSH 用户名
        private_key: SSH 私钥路径
    """
    manager = SSHTunnelManager(
        remote_host, remote_port, local_port,
        jump_host, jump_port, username, private_key
    )
    try:
        actual_port = manager.start()
        yield actual_port
    finally:
        manager.stop()


def test_tunnel(
    remote_host: str = "db.internal.example.com", 
    remote_port: int = 3306
) -> bool:
    """测试 SSH 隧道是否可连接。"""
    try:
        with tunnel_context(remote_host, remote_port) as port:
            logger.info("SSH 隧道测试成功，本地端口: {}", port)
            return True
    except Exception as e:
        logger.error("SSH 隧道测试失败: {}", e)
        return False
