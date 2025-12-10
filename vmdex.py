import discord
from discord.ext import commands
from discord import app_commands
import os
import random
import string
import subprocess
from dotenv import load_dotenv
import asyncio
import datetime
import docker
import logging
import aiohttp
import psutil
import shutil
import sqlite3

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('vmdex.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('VMDEX')

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
OWNER_ID = int(os.getenv('OWNER_ID', '0'))
HOST_IP = os.getenv('HOST_IP')
DB_FILE = 'vmdex.db'
DEFAULT_OS_IMAGE = 'ubuntu:24.04'
DOCKER_NETWORK = 'bridge'
PORT_RANGE_START = 20000
PORT_RANGE_END = 30000
EMBED_COLOR = discord.Color.from_rgb(0, 255, 255)
FOOTER_TEXT = "Coded by @Orbred"

DOCKERFILE_TEMPLATE = """
FROM {base_image}

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \\
    apt-get install -y systemd systemd-sysv dbus sudo \\
                       curl gnupg2 apt-transport-https ca-certificates \\
                       software-properties-common \\
                       docker.io openssh-server tmate && \\
    apt-get clean && rm -rf /var/lib/apt/lists/*

RUN echo "root:root" | chpasswd

RUN mkdir /var/run/sshd && \\
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \\
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config

RUN systemctl enable ssh && \\
    systemctl enable docker

RUN echo 'VMDEX Virtual Machine' > /etc/motd && \\
    echo 'vmdex-{vm_id}' > /etc/hostname

RUN apt-get update && \\
    apt-get install -y neofetch htop nano vim wget git tmux net-tools dnsutils iputils-ping && \\
    apt-get clean && \\
    rm -rf /var/lib/apt/lists/*

STOPSIGNAL SIGRTMIN+3

CMD ["/sbin/init"]
"""


class Database:
    def __init__(self, db_file):
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._create_tables()

    def _create_tables(self):
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS vm_instance (
                id INTEGER PRIMARY KEY,
                vm_id TEXT UNIQUE,
                container_id TEXT,
                memory INTEGER,
                cpu INTEGER,
                disk INTEGER,
                password TEXT,
                created_at TEXT,
                tmate_session TEXT,
                status TEXT DEFAULT 'running',
                external_ssh_port INTEGER,
                restart_count INTEGER DEFAULT 0,
                last_restart TEXT
            )
        ''')
        self.conn.commit()

    def get_vm(self):
        self.cursor.execute('SELECT * FROM vm_instance WHERE id = 1')
        row = self.cursor.fetchone()
        if not row:
            return None
        columns = [desc[0] for desc in self.cursor.description]
        return dict(zip(columns, row))

    def create_vm(self, vm_data):
        self.cursor.execute('DELETE FROM vm_instance')
        columns = ', '.join(vm_data.keys())
        placeholders = ', '.join('?' for _ in vm_data)
        self.cursor.execute(f'INSERT INTO vm_instance ({columns}) VALUES ({placeholders})', tuple(vm_data.values()))
        self.conn.commit()

    def update_vm(self, updates):
        if not updates:
            return False
        set_clause = ', '.join(f'{k} = ?' for k in updates)
        values = list(updates.values())
        self.cursor.execute(f'UPDATE vm_instance SET {set_clause} WHERE id = 1', values)
        self.conn.commit()
        return self.cursor.rowcount > 0

    def delete_vm(self):
        self.cursor.execute('DELETE FROM vm_instance WHERE id = 1')
        self.conn.commit()
        return self.cursor.rowcount > 0

    def get_used_port(self):
        self.cursor.execute('SELECT external_ssh_port FROM vm_instance WHERE id = 1')
        row = self.cursor.fetchone()
        return row[0] if row else None

    def close(self):
        self.conn.close()


class VMDEXBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db = Database(DB_FILE)
        self.session = None
        self.docker_client = None
        self.public_ip = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        try:
            self.docker_client = docker.from_env()
            logger.info("Docker client initialized")
            self.public_ip = HOST_IP or await self.get_public_ip()
            logger.info(f"Public IP: {self.public_ip}")
            await self.reconnect_container()
        except Exception as e:
            logger.error(f"Failed to initialize Docker client: {e}")
            self.docker_client = None

    async def get_public_ip(self):
        try:
            async with self.session.get('https://api.ipify.org') as resp:
                if resp.status == 200:
                    return await resp.text()
                return '127.0.0.1'
        except Exception as e:
            logger.error(f"Error getting public IP: {e}")
            return '127.0.0.1'

    async def reconnect_container(self):
        if not self.docker_client:
            return
        vm = self.db.get_vm()
        if vm and vm['status'] == 'running':
            try:
                container = self.docker_client.containers.get(vm['container_id'])
                if container.status != 'running':
                    container.start()
                logger.info(f"Reconnected to container for VM {vm['vm_id']}")
            except docker.errors.NotFound:
                logger.warning(f"Container {vm['container_id']} not found, removing from database")
                self.db.delete_vm()
            except Exception as e:
                logger.error(f"Error reconnecting container: {e}")

    async def close(self):
        await super().close()
        if self.session:
            await self.session.close()
        if self.docker_client:
            self.docker_client.close()
        self.db.close()


def generate_vm_id():
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))


def generate_password():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=12))


def get_available_port(db):
    used_port = db.get_used_port()
    while True:
        port = random.randint(PORT_RANGE_START, PORT_RANGE_END)
        if port != used_port:
            return port


async def capture_ssh_session_line(process):
    try:
        while True:
            output = await process.stdout.readline()
            if not output:
                break
            output = output.decode('utf-8').strip()
            if "ssh session:" in output:
                session_line = output.split("ssh session:")[1].strip()
                try:
                    process.kill()
                except:
                    pass
                return session_line
        return None
    except Exception as e:
        logger.error(f"Error capturing SSH session: {e}")
        return None
    finally:
        try:
            process.kill()
        except:
            pass


async def run_docker_command(container_id, command, timeout=120):
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "exec", container_id, *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            if process.returncode != 0:
                raise Exception(f"Command failed: {stderr.decode()}")
            return True, stdout.decode()
        except asyncio.TimeoutError:
            process.kill()
            raise Exception(f"Command timed out after {timeout} seconds")
    except Exception as e:
        logger.error(f"Error running Docker command: {e}")
        return False, str(e)


async def build_custom_image(vm_id):
    try:
        temp_dir = f"temp_dockerfiles/{vm_id}"
        os.makedirs(temp_dir, exist_ok=True)
        dockerfile_content = DOCKERFILE_TEMPLATE.format(
            base_image=DEFAULT_OS_IMAGE,
            vm_id=vm_id
        )
        dockerfile_path = os.path.join(temp_dir, "Dockerfile")
        with open(dockerfile_path, 'w') as f:
            f.write(dockerfile_content)
        image_tag = f"vmdex/{vm_id.lower()}:latest"
        build_process = await asyncio.create_subprocess_exec(
            "docker", "build", "-t", image_tag, temp_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await build_process.communicate()
        if build_process.returncode != 0:
            raise Exception(f"Failed to build image: {stderr.decode()}")
        return image_tag
    except Exception as e:
        logger.error(f"Error building custom image: {e}")
        raise
    finally:
        try:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
        except Exception as e:
            logger.error(f"Error cleaning up temp directory: {e}")


def create_embed(title, description=None, fields=None):
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)


intents = discord.Intents.default()
intents.message_content = True
bot = VMDEXBot(command_prefix='/', intents=intents, help_command=None)


@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord')
    vm = bot.db.get_vm()
    if vm and vm['status'] == 'running' and bot.docker_client:
        try:
            container = bot.docker_client.containers.get(vm['container_id'])
            if container.status != 'running':
                container.start()
                logger.info(f"Started container for VM {vm['vm_id']}")
        except docker.errors.NotFound:
            logger.warning(f"Container not found")
        except Exception as e:
            logger.error(f"Error starting container: {e}")
    try:
        await bot.change_presence(activity=discord.Activity(
            type=discord.ActivityType.watching, 
            name="VMDEX Environment..."
        ))
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")


@bot.hybrid_command(name='help', description='Show all available commands')
async def help_command(ctx):
    embed = create_embed(
        title="VMDEX - ECHO Commands",
        description="Virtual Machine Management System"
    )
    commands_list = """
`/deploy` - Deploy a new virtual machine
`/start` - Start the virtual machine
`/stop` - Stop the virtual machine
`/restart` - Restart the virtual machine
`/connect` - Get connection details for the VM
`/status` - View VM status and resource usage
`/delete` - Delete the virtual machine
`/help` - Show this help message
"""
    embed.add_field(name="Available Commands", value=commands_list, inline=False)
    await ctx.send(embed=embed)


@bot.hybrid_command(name='deploy', description='Deploy a new virtual machine')
@app_commands.describe(
    memory="Memory in GB (1-64)",
    cpu="CPU cores (1-16)",
    disk="Disk space in GB (10-500)"
)
@is_owner()
async def deploy(ctx, memory: int = 2, cpu: int = 1, disk: int = 20):
    if not bot.docker_client:
        embed = create_embed("Deployment Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    existing_vm = bot.db.get_vm()
    if existing_vm:
        embed = create_embed("Deployment Failed", "A virtual machine already exists. Delete it first using /delete.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    if memory < 1 or memory > 64:
        embed = create_embed("Deployment Failed", "Memory must be between 1GB and 64GB.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    if cpu < 1 or cpu > 16:
        embed = create_embed("Deployment Failed", "CPU cores must be between 1 and 16.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    if disk < 10 or disk > 500:
        embed = create_embed("Deployment Failed", "Disk space must be between 10GB and 500GB.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    status_embed = create_embed("Deploying Virtual Machine", "Initializing deployment process...")
    status_msg = await ctx.send(embed=status_embed)

    try:
        vm_id = generate_vm_id()
        password = generate_password()
        external_port = get_available_port(bot.db)
        memory_bytes = memory * 1024 * 1024 * 1024

        status_embed = create_embed("Deploying Virtual Machine", "Building custom Docker image...")
        await status_msg.edit(embed=status_embed)
        
        image_tag = await build_custom_image(vm_id)

        status_embed = create_embed("Deploying Virtual Machine", "Creating container...")
        await status_msg.edit(embed=status_embed)

        container = bot.docker_client.containers.run(
            image_tag,
            detach=True,
            privileged=True,
            hostname=f"vmdex-{vm_id}",
            mem_limit=memory_bytes,
            cpu_period=100000,
            cpu_quota=int(cpu * 100000),
            cap_add=["ALL"],
            network=DOCKER_NETWORK,
            ports={'22/tcp': str(external_port)},
            volumes={f'vmdex-{vm_id}': {'bind': '/data', 'mode': 'rw'}},
            restart_policy={"Name": "always"}
        )

        await asyncio.sleep(5)

        status_embed = create_embed("Deploying Virtual Machine", "Configuring SSH access...")
        await status_msg.edit(embed=status_embed)

        await run_docker_command(container.id, ["bash", "-c", f"echo 'root:{password}' | chpasswd"])

        status_embed = create_embed("Deploying Virtual Machine", "Starting tmate session...")
        await status_msg.edit(embed=status_embed)

        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container.id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        ssh_session_line = await capture_ssh_session_line(exec_cmd)

        vm_data = {
            "id": 1,
            "vm_id": vm_id,
            "container_id": container.id,
            "memory": memory,
            "cpu": cpu,
            "disk": disk,
            "password": password,
            "created_at": str(datetime.datetime.now()),
            "tmate_session": ssh_session_line or "Session not available",
            "status": "running",
            "external_ssh_port": external_port,
            "restart_count": 0,
            "last_restart": None
        }
        
        bot.db.create_vm(vm_data)

        fields = [
            ("VM ID", vm_id, True),
            ("Memory", f"{memory}GB", True),
            ("CPU", f"{cpu} cores", True),
            ("Disk", f"{disk}GB", True),
            ("OS", "Ubuntu 24.04", True),
            ("Status", "Running", True),
            ("Username", "root", True),
            ("Password", f"||{password}||", True),
            ("SSH Port", str(external_port), True),
        ]
        
        if ssh_session_line:
            fields.append(("Tmate Session", f"```{ssh_session_line}```", False))
        
        fields.append(("Direct SSH", f"```ssh root@{bot.public_ip} -p {external_port}```", False))

        success_embed = create_embed("Virtual Machine Deployed Successfully", None, fields)
        await status_msg.edit(embed=success_embed)

        try:
            dm_embed = create_embed("VMDEX - VM Credentials", "Your virtual machine has been deployed.", fields)
            await ctx.author.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    except Exception as e:
        logger.error(f"Error deploying VM: {e}")
        error_embed = create_embed("Deployment Failed", f"An error occurred: {str(e)}")
        await status_msg.edit(embed=error_embed)
        if 'container' in locals():
            try:
                container.stop()
                container.remove()
            except:
                pass


@bot.hybrid_command(name='start', description='Start the virtual machine')
@is_owner()
async def start(ctx):
    if not bot.docker_client:
        embed = create_embed("Start Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    vm = bot.db.get_vm()
    if not vm:
        embed = create_embed("Start Failed", "No virtual machine exists. Deploy one first using /deploy.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    try:
        container = bot.docker_client.containers.get(vm['container_id'])
        if container.status == 'running':
            embed = create_embed("Already Running", "The virtual machine is already running.")
            await ctx.send(embed=embed, ephemeral=True)
            return
        
        container.start()
        await asyncio.sleep(3)
        bot.db.update_vm({'status': 'running'})
        
        embed = create_embed("Virtual Machine Started", f"VM {vm['vm_id']} has been started successfully.")
        await ctx.send(embed=embed)
    except docker.errors.NotFound:
        bot.db.delete_vm()
        embed = create_embed("Start Failed", "Container not found. The VM data has been cleaned up.")
        await ctx.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error starting VM: {e}")
        embed = create_embed("Start Failed", f"An error occurred: {str(e)}")
        await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name='stop', description='Stop the virtual machine')
@is_owner()
async def stop(ctx):
    if not bot.docker_client:
        embed = create_embed("Stop Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    vm = bot.db.get_vm()
    if not vm:
        embed = create_embed("Stop Failed", "No virtual machine exists.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    try:
        container = bot.docker_client.containers.get(vm['container_id'])
        if container.status != 'running':
            embed = create_embed("Already Stopped", "The virtual machine is already stopped.")
            await ctx.send(embed=embed, ephemeral=True)
            return
        
        container.stop(timeout=30)
        bot.db.update_vm({'status': 'stopped'})
        
        embed = create_embed("Virtual Machine Stopped", f"VM {vm['vm_id']} has been stopped successfully.")
        await ctx.send(embed=embed)
    except docker.errors.NotFound:
        bot.db.delete_vm()
        embed = create_embed("Stop Failed", "Container not found. The VM data has been cleaned up.")
        await ctx.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error stopping VM: {e}")
        embed = create_embed("Stop Failed", f"An error occurred: {str(e)}")
        await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name='restart', description='Restart the virtual machine')
@is_owner()
async def restart(ctx):
    if not bot.docker_client:
        embed = create_embed("Restart Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    vm = bot.db.get_vm()
    if not vm:
        embed = create_embed("Restart Failed", "No virtual machine exists.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    status_embed = create_embed("Restarting Virtual Machine", "Please wait...")
    status_msg = await ctx.send(embed=status_embed)

    try:
        container = bot.docker_client.containers.get(vm['container_id'])
        container.restart()
        await asyncio.sleep(5)
        
        restart_count = vm.get('restart_count', 0) + 1
        bot.db.update_vm({
            'status': 'running',
            'restart_count': restart_count,
            'last_restart': str(datetime.datetime.now())
        })

        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", container.id, "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if ssh_session_line:
            bot.db.update_vm({'tmate_session': ssh_session_line})

        fields = [
            ("VM ID", vm['vm_id'], True),
            ("Status", "Running", True),
            ("Restart Count", str(restart_count), True),
        ]
        if ssh_session_line:
            fields.append(("New Tmate Session", f"```{ssh_session_line}```", False))
        fields.append(("Direct SSH", f"```ssh root@{bot.public_ip} -p {vm['external_ssh_port']}```", False))

        success_embed = create_embed("Virtual Machine Restarted", None, fields)
        await status_msg.edit(embed=success_embed)

    except docker.errors.NotFound:
        bot.db.delete_vm()
        embed = create_embed("Restart Failed", "Container not found. The VM data has been cleaned up.")
        await status_msg.edit(embed=embed)
    except Exception as e:
        logger.error(f"Error restarting VM: {e}")
        embed = create_embed("Restart Failed", f"An error occurred: {str(e)}")
        await status_msg.edit(embed=embed)


@bot.hybrid_command(name='connect', description='Get connection details for the virtual machine')
@is_owner()
async def connect(ctx):
    if not bot.docker_client:
        embed = create_embed("Connection Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    vm = bot.db.get_vm()
    if not vm:
        embed = create_embed("Connection Failed", "No virtual machine exists.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    try:
        container = bot.docker_client.containers.get(vm['container_id'])
        if container.status != 'running':
            container.start()
            await asyncio.sleep(5)
            bot.db.update_vm({'status': 'running'})

        exec_cmd = await asyncio.create_subprocess_exec(
            "docker", "exec", vm['container_id'], "tmate", "-F",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        ssh_session_line = await capture_ssh_session_line(exec_cmd)
        if ssh_session_line:
            bot.db.update_vm({'tmate_session': ssh_session_line})

        fields = [
            ("VM ID", vm['vm_id'], True),
            ("Status", "Running", True),
            ("Username", "root", True),
            ("Password", f"||{vm['password']}||", True),
            ("SSH Port", str(vm['external_ssh_port']), True),
            ("OS", "Ubuntu 24.04", True),
        ]
        
        if ssh_session_line:
            fields.append(("Tmate Session", f"```{ssh_session_line}```", False))
        
        fields.append(("Direct SSH", f"```ssh root@{bot.public_ip} -p {vm['external_ssh_port']}```", False))
        
        instructions = """
1. Copy the SSH command above
2. Open your terminal
3. Paste and run the command
4. Enter the password when prompted
"""
        fields.append(("Connection Instructions", instructions, False))

        embed = create_embed("VM Connection Details", None, fields)
        
        try:
            await ctx.author.send(embed=embed)
            response_embed = create_embed("Connection Details Sent", "Check your direct messages for VM connection details.")
            await ctx.send(embed=response_embed, ephemeral=True)
        except discord.Forbidden:
            await ctx.send(embed=embed, ephemeral=True)

    except docker.errors.NotFound:
        bot.db.delete_vm()
        embed = create_embed("Connection Failed", "Container not found. The VM data has been cleaned up.")
        await ctx.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error connecting to VM: {e}")
        embed = create_embed("Connection Failed", f"An error occurred: {str(e)}")
        await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name='status', description='View VM status and resource usage')
@is_owner()
async def status(ctx):
    if not bot.docker_client:
        embed = create_embed("Status Check Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    vm = bot.db.get_vm()
    if not vm:
        embed = create_embed("No Virtual Machine", "No virtual machine exists. Deploy one using /deploy.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    try:
        container = bot.docker_client.containers.get(vm['container_id'])
        container_status = container.status.capitalize()
        
        mem_output = ""
        disk_output = ""
        
        if container.status == 'running':
            try:
                mem_process = await asyncio.create_subprocess_exec(
                    "docker", "exec", vm['container_id'], "free", "-m",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await mem_process.communicate()
                if mem_process.returncode == 0:
                    mem_output = stdout.decode()
            except:
                pass
            
            try:
                disk_process = await asyncio.create_subprocess_exec(
                    "docker", "exec", vm['container_id'], "df", "-h", "/",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await disk_process.communicate()
                if disk_process.returncode == 0:
                    disk_output = stdout.decode()
            except:
                pass

        fields = [
            ("VM ID", vm['vm_id'], True),
            ("Status", container_status, True),
            ("OS", "Ubuntu 24.04", True),
            ("Allocated Memory", f"{vm['memory']}GB", True),
            ("Allocated CPU", f"{vm['cpu']} cores", True),
            ("Allocated Disk", f"{vm['disk']}GB", True),
            ("SSH Port", str(vm['external_ssh_port']), True),
            ("Restart Count", str(vm.get('restart_count', 0)), True),
            ("Created", vm['created_at'][:19], True),
        ]
        
        if vm.get('last_restart'):
            fields.append(("Last Restart", vm['last_restart'][:19], True))
        
        if mem_output:
            fields.append(("Memory Usage", f"```{mem_output}```", False))
        
        if disk_output:
            fields.append(("Disk Usage", f"```{disk_output}```", False))

        embed = create_embed("Virtual Machine Status", None, fields)
        await ctx.send(embed=embed)

    except docker.errors.NotFound:
        bot.db.update_vm({'status': 'not_found'})
        embed = create_embed("Container Not Found", "The container for this VM no longer exists.")
        await ctx.send(embed=embed, ephemeral=True)
    except Exception as e:
        logger.error(f"Error getting VM status: {e}")
        embed = create_embed("Status Check Failed", f"An error occurred: {str(e)}")
        await ctx.send(embed=embed, ephemeral=True)


@bot.hybrid_command(name='delete', description='Delete the virtual machine')
@is_owner()
async def delete(ctx):
    if not bot.docker_client:
        embed = create_embed("Delete Failed", "Docker is not available. Please contact the administrator.")
        await ctx.send(embed=embed, ephemeral=True)
        return
    
    vm = bot.db.get_vm()
    if not vm:
        embed = create_embed("Delete Failed", "No virtual machine exists.")
        await ctx.send(embed=embed, ephemeral=True)
        return

    confirm_embed = create_embed(
        "Confirm Deletion",
        f"Are you sure you want to delete VM {vm['vm_id']}? This action cannot be undone."
    )
    
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.confirmed = False

        @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != OWNER_ID:
                await interaction.response.send_message("You do not have permission.", ephemeral=True)
                return
            
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            
            try:
                container = bot.docker_client.containers.get(vm['container_id'])
                container.stop()
                container.remove()
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.error(f"Error removing container: {e}")
            
            bot.db.delete_vm()
            
            success_embed = create_embed("Virtual Machine Deleted", f"VM {vm['vm_id']} has been deleted successfully.")
            await interaction.response.edit_message(embed=success_embed, view=self)
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
            if interaction.user.id != OWNER_ID:
                await interaction.response.send_message("You do not have permission.", ephemeral=True)
                return
            
            for item in self.children:
                item.disabled = True
            
            cancel_embed = create_embed("Deletion Cancelled", "The virtual machine was not deleted.")
            await interaction.response.edit_message(embed=cancel_embed, view=self)
            self.stop()

    view = ConfirmView()
    await ctx.send(embed=confirm_embed, view=view)


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        embed = create_embed("Access Denied", "You do not have permission to use this command.")
        await ctx.send(embed=embed, ephemeral=True)
    elif isinstance(error, commands.CommandNotFound):
        embed = create_embed("Command Not Found", "Use /help to see available commands.")
        await ctx.send(embed=embed, ephemeral=True)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = create_embed("Missing Argument", f"Missing required argument: {error.param.name}")
        await ctx.send(embed=embed, ephemeral=True)
    else:
        logger.error(f"Command error: {error}")
        embed = create_embed("Error", f"An error occurred: {str(error)}")
        await ctx.send(embed=embed, ephemeral=True)


if __name__ == "__main__":
    os.makedirs("temp_dockerfiles", exist_ok=True)
    
    if not TOKEN:
        logger.error("DISCORD_TOKEN not set")
        print("Error: DISCORD_TOKEN environment variable is not set.")
        exit(1)
    
    if OWNER_ID == 0:
        logger.error("OWNER_ID not set")
        print("Error: OWNER_ID environment variable is not set.")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.error(f"Bot crashed: {e}")
