def myTestRule(rule_args, callback):
    cpu_weight = global_vars['*Cpuw'][1:-1]
    mem_weight = global_vars['*Memw'][1:-1]
    swap_weight = global_vars['*Swapw'][1:-1]
    run_queue_weight = global_vars['*Runw'][1:-1]
    disk_weight = global_vars['*Diskw'][1:-1]
    network_in_weight = global_vars['*Netinw'][1:-1]
    network_out_weight = global_vars['*Netow'][1:-1]

    callback.msiDigestMonStat(cpu_weight, mem_weight, swap_weight, run_queue_weight, disk_weight, network_in_weight, network_out_weight)

    callback.writeLine('stdout', 'CPU weight is ' + cpu_weight + ', Memory weight is ' + mem_weight + ', Swap weight is ' + swap_weight + ', Run queue weight is ' + run_queue_weight)
    callback.writeLine('stdout', 'Disk weight is ' + disk_weight + ', Network transfer in weight is ' + network_in_weight + ', Network transfer out weight is ' + network_out_weight)

    genQueryOut = {}
    genQueryOut[PYTHON_MSPARAM_TYPE] = PYTHON_GENQUERYOUT_MS_T
    ret_val = callback.msiExecStrCondQuery('SELECT SLD_RESC_NAME, SLD_LOAD_FACTOR', genQueryOut)
    genQueryOut = ret_val[PYTHON_RE_RET_OUTPUT][1]

    for row in range(int(genQueryOut['rowCnt'])):
        resc_name_str = 'value_' + str(row) + '_0'
        load_factor_str = 'value_' + str(row) + '_1'
        resc_name = genQueryOut[resc_name_str]
        load_factor = genQueryOut[load_factor_str]
        callback.writeLine('stdout', resc_name + ' = ' + load_factor)

INPUT *Cpuw="1", *Memw="1", *Swapw="0", *Runw="0", *Diskw="0", *Netinw="1", *Netow="1"
OUTPUT ruleExecOut