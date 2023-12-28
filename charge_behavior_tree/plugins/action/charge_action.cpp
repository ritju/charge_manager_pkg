
#include "charge_behavior_tree/plugins/action/charge_action.hpp"

namespace charge_behavior_tree
{
ChargeAction::ChargeAction(
        const std::string & xml_tag_name,
        const std::string & action_name,
        const BT::NodeConfiguration & conf)
        :nav2_behavior_tree::BtActionNode<charge_manager_msgs::action::Charge>(xml_tag_name, action_name, conf)
{
}

void ChargeAction::on_tick()
{
        if(!getInput("mac", goal_.mac))
        {
                RCLCPP_ERROR(node_->get_logger(), "Charge action: goal not provided.");
                return;
        }
        getInput("behavior_tree", goal_.behavior_tree);
}


} // end of namespace charge_behavior_tree

#include "behaviortree_cpp_v3/bt_factory.h"
BT_REGISTER_NODES(factory)
{
        BT::NodeBuilder builder = [](const std::string & name, const BT::NodeConfiguration & config)
        {
                return std::make_unique<charge_behavior_tree::ChargeAction>(name, "charge", config);
        };

        factory.registerBuilder<charge_behavior_tree::ChargeAction>("Charge", builder);
}




