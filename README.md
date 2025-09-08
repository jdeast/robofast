This code is (will be) a generic robotic observatory code that autonomously executes 
astronomical observations. 

New observatories must write new configuration files (see config/* for examples) 
and hardware abstraction layers (see hal/* for examples) that translate the specific 
hardware (e.g., paramount, Cdk700) into generic commands (slew). As such, these 
HAL classes must expose standardized function names. See existing codes for examples.

If you write code to support new hardware, please consider a pull request or sending
it to me so other users can use (and improve) it.
