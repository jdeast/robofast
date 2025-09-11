This code is (will be) a generic robotic observatory code that autonomously executes 
astronomical observations. 

New observatories must write new configuration files (see config/* for examples) 
and Hardware Abstraction Layer (HAL) classes (see hal/*.py for examples) that 
translate the specific hardware (e.g., Paramount, Cdk700) into generic commands 
(slew). As such, these HAL classes must expose standardized function names, which
are enforced by the high-level wrappers (e.g., telescope.py). See existing codes 
for examples.

After that, they can take advantage of the generalized robotic code and all of its 
infrastructure.

If you write code to support new hardware, please consider a pull request or sending
it to me so other users can use (and improve) it.
